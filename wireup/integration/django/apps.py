import asyncio
import functools
import importlib
from contextvars import ContextVar
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Type, Union

import django
import django.urls
from django.apps import AppConfig, apps
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver
from django.utils.decorators import sync_and_async_middleware
from django.views.generic import View
from rest_framework.views import APIView as DrfAPIView

import wireup
from wireup import service
from wireup._decorators import inject_from_container
from wireup.errors import WireupError
from wireup.ioc.container.async_container import AsyncContainer, ScopedAsyncContainer, async_container_force_sync_scope
from wireup.ioc.container.sync_container import ScopedSyncContainer
from wireup.ioc.types import ParameterWrapper
from wireup.ioc.validation import get_valid_injection_annotated_parameters

if TYPE_CHECKING:
    from wireup.integration.django import WireupSettings


current_request: ContextVar[HttpRequest] = ContextVar("wireup_django_request")
async_view_request_container: ContextVar[ScopedAsyncContainer] = ContextVar(
    "wireup_async_view_request_container"
)
sync_view_request_container: ContextVar[ScopedSyncContainer] = ContextVar(
    "wireup_sync_view_request_container"
)


@sync_and_async_middleware
def wireup_middleware(
    get_response: Callable[[HttpRequest], HttpResponse],
) -> Callable[[HttpRequest], Union[HttpResponse, Awaitable[HttpResponse]]]:
    container = get_app_container()

    if asyncio.iscoroutinefunction(get_response):

        async def async_inner(request: HttpRequest) -> HttpResponse:
            async with container.enter_scope() as scoped:
                container_token = async_view_request_container.set(scoped)
                request_token = current_request.set(request)
                try:
                    return await get_response(request)
                finally:
                    current_request.reset(request_token)
                    async_view_request_container.reset(container_token)

        return async_inner

    def sync_inner(request: HttpRequest) -> HttpResponse:
        with async_container_force_sync_scope(container) as scoped:
            container_token = sync_view_request_container.set(scoped)
            request_token = current_request.set(request)
            try:
                return get_response(request)
            finally:
                current_request.reset(request_token)
                sync_view_request_container.reset(container_token)

    return sync_inner


@service
def _django_request_factory() -> HttpRequest:
    try:
        return current_request.get()
    except LookupError as e:
        msg = (
            "django.http.HttpRequest in wireup is only available during a request. "
            "Did you forget to add 'wireup.integration.django.wireup_middleware' to your list of middlewares?"
        )
        raise WireupError(msg) from e


def get_request_container() -> Union[ScopedSyncContainer, ScopedAsyncContainer]:
    """When inside a request, returns the scoped container instance handling the current request."""
    try:
        return async_view_request_container.get()
    except LookupError:
        return sync_view_request_container.get()


def get_app_container() -> AsyncContainer:
    """Return the container instance associated with the current django application."""
    return apps.get_app_config(WireupConfig.name).container  # type: ignore[reportAttributeAccessIssue]


class WireupConfig(AppConfig):
    """Integrate wireup with Django."""

    name = "wireup"

    def __init__(self, app_name: str, app_module: Any) -> None:
        super().__init__(app_name, app_module)

    def ready(self) -> None:
        integration_settings: WireupSettings = settings.WIREUP

        self.container = wireup.create_async_container(
            service_modules=[
                importlib.import_module(m) if isinstance(m, str) else m for m in integration_settings.service_modules
            ],
            services=[_django_request_factory],
            parameters={
                entry: getattr(settings, entry)
                for entry in dir(settings)
                if not entry.startswith("__") and hasattr(settings, entry)
            },
        )
        self.inject_scoped = inject_from_container(self.container, get_request_container)

        self._inject(django.urls.get_resolver())

    def _inject(self, resolver: URLResolver) -> None:
        for p in resolver.url_patterns:
            if isinstance(p, URLResolver):
                self._inject(p)
                continue

            if isinstance(p, URLPattern) and p.callback:  # type: ignore[reportUnnecessaryComparison]
                if hasattr(p.callback, "view_class") and hasattr(p.callback, "view_initkwargs"):
                    p.callback = self._inject_django_class_based_view(p.callback)
                elif hasattr(p.callback, "cls") and issubclass(p.callback.cls, DrfAPIView):
                    p.callback = self._inject_drf_class_based_view(p.callback)
                else:
                    p.callback = self.inject_scoped(p.callback)

    def _inject_django_class_based_view(self, callback: Any) -> Any:
        # This is taken from the django .as_view() method.
        @functools.wraps(callback)
        def view(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            handler = self._get_handler(request, callback.view_class)
            injected_names = self._get_injected_names(handler)

            this = callback.view_class(**callback.view_initkwargs, **injected_names)
            this.setup(request, *args, **kwargs)
            self._assert_request_attribute(this)
            return this.dispatch(request, *args, **kwargs, **injected_names)

        return view

    def _inject_drf_class_based_view(self, callback: Any) -> Any:
        actions, klass, initkwargs = callback.actions, callback.cls, callback.initkwargs

        # This is taken from the django .as_view() method.
        @functools.wraps(callback)
        def view(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            this = klass(**initkwargs)

            if "get" in actions and "head" not in actions:
                actions["head"] = actions["get"]

            this.action_map = actions

            for method, action in actions.items():
                setattr(this, method, getattr(this, action))

            this.request = request
            this.args = args
            this.kwargs = kwargs

            self._assert_request_attribute(this)

            handler = self._get_handler(request, this)
            injected_names = self._get_injected_names(handler)
            return this.dispatch(request, *args, **kwargs, **injected_names)

        return view

    # helpers
    def _assert_request_attribute(self, this: Any) -> None:
        if not hasattr(this, "request"):
            raise AttributeError(
                "{} instance has no 'request' attribute. Did you override "  # noqa: EM103, UP032
                "setup() and forget to call super()?".format(this.__class__.__name__)
            )

    def _get_injected_names(self, handler: Callable) -> dict[str, Any]:
        names_to_inject = get_valid_injection_annotated_parameters(
            self.container, handler
        )
        return {
            name: (
                self.container.params.get(param.annotation.param)
                if isinstance(param.annotation, ParameterWrapper)
                else get_request_container().get(
                    param.klass, qualifier=param.qualifier_value
                )
            )
            for name, param in names_to_inject.items()
            if param.annotation
        }

    def _get_handler(self, request: HttpRequest, klass: Type[View]) -> Callable:
        # This is taken from dispatch method of django/drf view class.
        if request.method.lower() in klass.http_method_names:
            handler = getattr(
                klass, request.method.lower(), klass.http_method_not_allowed
            )
        else:
            handler = klass.http_method_not_allowed
        return handler
    #

@dataclass(frozen=True)
class WireupSettings:
    """Class containing Wireup settings specific to Django."""

    service_modules: List[Union[str, ModuleType]]
    """List of modules containing wireup service registrations."""

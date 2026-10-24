"""Native text protocol presentation over the same controlled Monitor ingress."""
from contextvars import ContextVar

from cloudyfan_jev.http_api import ServiceError
from cloudyfan_jev.ollama_api import OllamaApp
from cloudyfan_jev.scores.categories import resolve_category

from .store import MonitorError

_REQUEST = ContextVar("monitor_ollama_request", default=None)


class OllamaGateway:
    def __init__(self, gateway, *, models, authenticate):
        if not isinstance(models, dict) or not models:
            raise ValueError("Explicit native aliases required")
        self.gateway, self.authenticate = gateway, authenticate
        aliases, self.categories = {}, {}
        for alias, definition in models.items():
            if not isinstance(definition, dict) or set(definition) != {"category", "contract_id"}:
                raise ValueError("Native aliases require a category and exact registered contract")
            try:
                category = resolve_category(definition["category"])
            except ValueError:
                raise ValueError("Native alias category is not registered") from None
            contract = definition["contract_id"]
            if (not isinstance(contract, str) or contract not in gateway.service.contracts
                    or not any(c == category and target == contract for (_, c, _), target in gateway.registry.items())
                    or (contract in self.categories and self.categories[contract] != category)):
                raise ValueError("Native alias must bind an exact category runtime contract")
            aliases[alias], self.categories[contract] = contract, category
        self.app = OllamaApp(gateway.service, models=aliases, authenticate=self._authenticate,
                             submit_task=self._submit)

    def _authenticate(self, environ):
        try:
            return self.authenticate(environ)
        except MonitorError as error:
            raise ServiceError(error.code, error.status) from None

    def _submit(self, principal, payload):
        try:
            state = self.gateway.create(principal, {"run_id": payload["run_id"],
                "category": self.categories[payload["contract_id"]], "prompt": payload["prompt"]},
                expected_contract=payload["contract_id"])
            context = _REQUEST.get()
            binding = self.gateway.store.binding(principal, payload["run_id"])
            self.gateway.store.record(binding, {"ingress": {
                **context["environ"]["cloudyfan.ollama_request"], "received_at": context["received_at"]}})
            return state
        except MonitorError as error:
            raise ServiceError(error.code, error.status) from None

    def __call__(self, environ, start_response):
        token = _REQUEST.set({"environ": environ, "received_at": self.gateway.store.clock()})
        try:
            return self.app(environ, start_response)
        finally:
            _REQUEST.reset(token)

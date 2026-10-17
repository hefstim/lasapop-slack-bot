"""Thin wrapper around the Odoo 19 JSON-2 API."""
from config import Config
from odoo.json2 import OdooError, OdooJson2

# Safe to retry after a transport failure: they cannot have changed anything.
_READ_METHODS = {"search_read", "search", "search_count", "read", "read_group", "fields_get"}


class OdooClient:
    def __init__(self):
        self.url = Config.ODOO_URL.rstrip("/")
        self.db = Config.ODOO_DB
        self._odoo = OdooJson2(self.url, self.db, Config.ODOO_API_KEY)
        self._uid = None

    @property
    def uid(self) -> int:
        if self._uid is None:
            try:
                self._uid = self._odoo.uid()
            except OdooError as e:
                if e.status == 401:
                    raise ConnectionError("Odoo authentication failed. Check ODOO_API_KEY.") from e
                raise
        return self._uid

    def execute(self, model: str, method: str, *args, **kwargs):
        """Call a model method with XML-RPC style positional args.

        Each call is a separate stateless HTTP request (thread-safe). Read
        methods are retried once on a transport error such as an Odoo restart.
        """
        try:
            return self._odoo.execute_kw(model, method, list(args), kwargs)
        except OdooError as e:
            if e.status is None and method in _READ_METHODS:
                return self._odoo.execute_kw(model, method, list(args), kwargs)
            raise

    def search_read(self, model: str, domain: list, fields: list,
                    limit: int = 0, order: str = "") -> list:
        kwargs = {"fields": fields}
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.execute(model, "search_read", domain, **kwargs)

    def read_group(self, model: str, domain: list, fields: list, groupby: list) -> list:
        return self.execute(model, "read_group", domain, fields, groupby)

    def ping(self) -> bool:
        try:
            return bool(self.uid)
        except Exception:
            return False


# Singleton
_client: OdooClient | None = None


def get_client() -> OdooClient:
    global _client
    if _client is None:
        _client = OdooClient()
    return _client

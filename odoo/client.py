"""Thin wrapper around Odoo XML-RPC API."""
import http.client
import xmlrpc.client
from config import Config


class OdooClient:
    def __init__(self):
        self.url = Config.ODOO_URL.rstrip("/")
        self.db = Config.ODOO_DB
        self.username = Config.ODOO_USERNAME
        self.password = Config.ODOO_PASSWORD
        self._uid = None
        self._connect()

    def _connect(self):
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    @property
    def uid(self) -> int:
        if self._uid is None:
            self._uid = self._common.authenticate(self.db, self.username, self.password, {})
            if not self._uid:
                raise ConnectionError("Odoo authentication failed. Check credentials.")
        return self._uid

    def execute(self, model: str, method: str, *args, **kwargs):
        """Run an XML-RPC call, retrying once on a broken persistent connection.

        ServerProxy caches its socket for the process lifetime, so once it
        times out or the server drops it mid-request (surfaces as
        http.client errors like "Request-sent" or "Idle"), every later call
        keeps failing the same way until the connection is recreated.
        """
        try:
            return self._models.execute_kw(
                self.db, self.uid, self.password,
                model, method, list(args), kwargs
            )
        except (http.client.HTTPException, OSError):
            self._connect()
            return self._models.execute_kw(
                self.db, self.uid, self.password,
                model, method, list(args), kwargs
            )

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
            version = self._common.version()
            return bool(version)
        except Exception:
            return False


# Singleton
_client: OdooClient | None = None


def get_client() -> OdooClient:
    global _client
    if _client is None:
        _client = OdooClient()
    return _client

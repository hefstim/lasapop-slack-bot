"""Minimal Odoo 19 JSON-2 API client (stdlib only).

POST {url}/json/2/<model>/<method> with a bearer API key. Arguments are
passed by name; record ids go in "ids". See
https://www.odoo.com/documentation/19.0/developer/reference/external_api.html

`execute_kw()` accepts the old XML-RPC calling convention
(positional args list + kwargs dict) and maps positional args onto the
parameter names JSON-2 requires, so callers written for XML-RPC keep working.
"""
import json
import urllib.error
import urllib.request

# Positional parameter names of @api.model methods (called without ids).
_MODEL_METHOD_ARGS = {
    "search_read": ("domain", "fields", "offset", "limit", "order"),
    "search": ("domain", "offset", "limit", "order"),
    "search_count": ("domain", "limit"),
    "read_group": ("domain", "fields", "groupby", "offset", "limit", "orderby", "lazy"),
    "fields_get": ("allfields", "attributes"),
    "name_search": ("name", "domain", "operator", "limit"),
    "default_get": ("fields_list",),
    "create": ("vals_list",),
}
# Positional parameter names (after ids) of record methods. Any method not
# listed here or above is treated as a record method taking only ids.
_RECORD_METHOD_ARGS = {
    "read": ("fields", "load"),
    "write": ("vals",),
    "copy": ("default",),
}


class OdooError(Exception):
    """Error returned by the Odoo server (or transport failure)."""

    def __init__(self, message, status=None, name=None):
        super().__init__(message)
        self.status = status
        self.name = name


class OdooJson2:
    def __init__(self, url, db, api_key, timeout=60):
        self.url = url.rstrip("/")
        self.db = db
        self.api_key = api_key
        self.timeout = timeout

    def call(self, model, method, ids=None, **params):
        """Call `model.method` with named parameters; `ids` selects records."""
        if ids is not None:
            params["ids"] = list(ids)
        req = urllib.request.Request(
            f"{self.url}/json/2/{model}/{method}",
            data=json.dumps(params).encode(),
            headers={
                "Authorization": f"bearer {self.api_key}",
                "X-Odoo-Database": self.db,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "odoo-json2-client",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                err = json.loads(raw)
                name = err.get("name", "")
                message = err.get("message") or name or raw.decode(errors="replace")
            except ValueError:
                name, message = "", raw.decode(errors="replace")[:500]
            raise OdooError(f"{model}.{method}: HTTP {e.code} {message}", e.code, name) from None
        except urllib.error.URLError as e:
            raise OdooError(f"{model}.{method}: {e.reason}") from None
        return json.loads(body) if body else None

    def execute_kw(self, *call):
        """XML-RPC style call: positional `args` list plus `kwargs` dict.

        Accepts (model, method, args=(), kwargs=None) or the full XML-RPC
        shape (db, uid, password, model, method, args=(), kwargs=None), in
        which db/uid/password are ignored (the API key identifies the user).
        """
        if len(call) >= 5:
            call = call[3:]
        return self._execute_kw(*call)

    def _execute_kw(self, model, method, args=(), kwargs=None):
        args = list(args or [])
        params = dict(kwargs or {})
        ids = None
        if method in _MODEL_METHOD_ARGS:
            names = _MODEL_METHOD_ARGS[method]
        else:
            if args:
                ids = args.pop(0)
                if isinstance(ids, int):
                    ids = [ids]
            names = _RECORD_METHOD_ARGS.get(method, ())
        if len(args) > len(names):
            raise TypeError(f"{model}.{method}: too many positional arguments for JSON-2")
        for name, value in zip(names, args):
            if name in params:
                raise TypeError(f"{model}.{method}: got multiple values for {name!r}")
            params[name] = value
        result = self.call(model, method, ids=ids, **params)
        # XML-RPC create() with a single dict returns an int; JSON-2 a list.
        if method == "create" and isinstance(params.get("vals_list"), dict) and isinstance(result, list):
            return result[0]
        return result

    def uid(self):
        return self.call("res.users", "context_get")["uid"]

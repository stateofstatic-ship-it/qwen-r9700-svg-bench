"""Bounded stdlib OpenAI-compatible transport; never writes authorization headers."""
import json
import os
from urllib import request, error, parse

MAX_RESPONSE = 2 * 1024 * 1024

class EndpointError(RuntimeError):
    pass

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise EndpointError('Endpoint redirects are prohibited')

class Endpoint:
    def __init__(self, url, api_key_env='SVG_BENCH_API_KEY'):
        parts = parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise EndpointError('Endpoint must be an http(s) base URL without credentials, query or fragment')
        self.url = url.rstrip('/')
        self.key = os.environ.get(api_key_env, '')
        self.opener = request.build_opener(NoRedirect)

    def call(self, path, payload=None, timeout=20):
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if data is not None and len(data) > 8 * 1024 * 1024:
            raise EndpointError('Endpoint request/history exceeds 8 MiB')
        req = request.Request(self.url + path, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except error.HTTPError as exc:
            raise EndpointError('Endpoint returned HTTP ' + str(exc.code)) from None
        except (error.URLError, TimeoutError, OSError):
            raise EndpointError('Endpoint connection failed or timed out') from None
        if len(raw) > MAX_RESPONSE:
            raise EndpointError('Endpoint response exceeds 2 MiB')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise EndpointError('Endpoint response is not JSON') from None
        if not isinstance(value, dict) or 'error' in value:
            raise EndpointError('Endpoint returned an invalid object or API error')
        return value

    def select_model(self, model=None):
        metadata = self.call('/models')
        data = metadata.get('data')
        if not isinstance(data, list) or not data or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'] for x in data):
            raise EndpointError('GET /models has no valid model list')
        ids = [x['id'] for x in data]
        if model is None:
            if len(ids) != 1:
                raise EndpointError('Specify model when endpoint lists multiple models')
            model = ids[0]
        if model not in ids:
            raise EndpointError('Requested model is not listed by endpoint')
        return model, metadata


def list_models(endpoint, api_key_env='SVG_BENCH_API_KEY', timeout=5):
    """Metadata-only model IDs; does not test inference/tool-call compatibility."""
    metadata = Endpoint(endpoint, api_key_env).call('/models', timeout=timeout)
    data = metadata.get('data')
    if not isinstance(data, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'] for x in data):
        raise EndpointError('GET /models has no valid model list')
    return [x['id'] for x in data]

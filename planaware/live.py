"""One manually authorized N1 acquisition; no retries, polling or token cache."""
from datetime import datetime, timezone
from http.client import HTTPException
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .n1 import FrozenSnapshot, freeze_n1_fixture, parse_strict_json, persist_snapshot

TOKEN_URL = 'https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token'
N1_BASE = 'https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/City/'
MAX_BYTES = 524288


class PipelineError(Exception):
    """Only fixed, public diagnostics; never upstream body/headers/exception text."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(request, *, opener, timeout, limit, phase):
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise PipelineError(f'{phase}: unexpected HTTP status')
            if response.headers.get_content_type() not in ('application/json', 'application/problem+json'):
                raise PipelineError(f'{phase}: expected JSON content type')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise PipelineError(f'{phase}: unsupported content encoding')
            body = response.read(limit + 1)
            if len(body) > limit:
                raise PipelineError(f'{phase}: response exceeds byte limit')
            return body.decode('utf-8')
    except HTTPError as error:
        code = error.code
        error.close()
        # HTTP messages and bodies can echo credentials. Never include them.
        if code in (401, 403):
            message = 'authentication/authorization denied'
        elif code == 429:
            message = 'rate/quota limit; not retried'
        elif 500 <= code <= 599:
            message = 'server failure; not retried'
        else:
            message = 'HTTP request rejected; redirects disabled'
        raise PipelineError(f'{phase}: HTTP {code}; {message}') from None
    except (URLError, OSError, ValueError, HTTPException):
        raise PipelineError(f'{phase}: network/TLS/read/encoding failure; not retried') from None


def _bounded_url(scope, city, max_records):
    if not re.fullmatch(r'[A-Za-z]+', city) or len(city) > 32:
        raise PipelineError('Invalid city identifier')
    if type(max_records) is not int or not 1 <= max_records <= 100 or len(scope.stop_uids) > 10:
        raise PipelineError('Require 1..100 records and at most 10 selected stops')
    # Scope values become OData literals, not query syntax.
    def literal(value):
        return "'" + value.replace("'", "''") + "'"
    stops = ' or '.join('StopUID eq ' + literal(stop) for stop in scope.stop_uids)
    query = {'$filter': f'RouteUID eq {literal(scope.route_uid)} and Direction eq {scope.direction} and ({stops})',
             '$top': max_records + 1, '$format': 'JSON'}
    # One extra record detects truncation without a second/pagination request.
    return N1_BASE + city + '?' + urlencode(query)


def save_atomic(snapshot, path):
    """Publish a complete snapshot by hard link without replacing the destination."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.n1-', delete=False) as output:
            temporary = output.name
            output.write(snapshot.to_json() + '\n')
            os.fchmod(output.fileno(), 0o400)
        os.link(temporary, path)  # Atomic visibility; fails if ANY destination exists.
    except OSError:
        raise PipelineError('Artifact publication failed; existing artifacts are never replaced') from None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def psql_executor(database):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', database):
        raise PipelineError('Use a plain database name; connection secrets belong in libpq configuration')
    def execute(statement):
        env = {key: value for key, value in os.environ.items() if not key.startswith('TDX_')}
        try:
            result = subprocess.run(['psql', '-X', '-w', '-A', '-t', '-q',
                '-v', 'ON_ERROR_STOP=1', '--dbname', database], input=statement,
                text=True, capture_output=True, env=env, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise PipelineError('Database execution failed/timed out; replay saved artifact; commit outcome may be unknown') from None
        if result.returncode:
            raise PipelineError('Database write rejected; replay saved artifact after resolving connection/conflict')
        output = result.stdout.strip()
        if not re.fullmatch(r'[1-9][0-9]*', output):
            raise PipelineError('Unexpected database result; replay saved artifact to verify outcome')
        return output
    return execute


def replay(path, execute_sql):
    # No authentication or HTTP construction in the recovery path.
    try:
        saved = FrozenSnapshot.from_json(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        raise PipelineError('Cannot load valid frozen artifact') from None
    return _persist(saved, execute_sql, authentication_requests=0, data_requests=0)


def _persist(saved, execute_sql, *, authentication_requests, data_requests):
    try:
        run_id = persist_snapshot(saved, execute_sql)
    except Exception:
        # Database diagnostics may echo SQL/raw evidence; never print them.
        raise PipelineError('Database persistence failed; artifact retained for replay; outcome may be unknown') from None
    return {'run_key': saved.run_key, 'record_count': len(parse_strict_json(saved.records_json)),
            'database_run_id': run_id, 'authentication_requests': authentication_requests,
            'data_requests': data_requests}


def fetch_once(*, scope, city, max_records, output, execute_sql, confirm_free_quota,
               timeout=15, opener=None):
    if not confirm_free_quota:
        raise PipelineError('Confirm membership, free access and remaining quota before fetching')
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
        raise PipelineError('HTTP timeout must be 1..30 seconds')
    url = _bounded_url(scope, city, max_records)
    path = Path(output)
    if os.path.lexists(path) or not path.parent.is_dir():
        raise PipelineError('Output must be a new artifact in an existing directory')
    client_id, secret = os.environ.get('TDX_CLIENT_ID'), os.environ.get('TDX_CLIENT_SECRET')
    if not client_id or not secret:
        raise PipelineError('TDX_CLIENT_ID and TDX_CLIENT_SECRET are required in the environment')
    # Identity exists BEFORE authentication or the quota-consuming data fetch.
    run_key = 'tdx-n1:' + uuid4().hex
    opener = opener if opener is not None else build_opener(NoRedirect())
    token_request = Request(TOKEN_URL, data=urlencode({'grant_type': 'client_credentials',
        'client_id': client_id, 'client_secret': secret}).encode('ascii'),
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    auth = _request(token_request, opener=opener, timeout=timeout, limit=16384, phase='authentication')
    try:
        token_data = parse_strict_json(auth)
        token = token_data['access_token']
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', token) or token_data.get('token_type', '').lower() != 'bearer':
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PipelineError('Invalid authentication response') from None
    requested_at = datetime.now(timezone.utc).isoformat()
    request = Request(url, headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json', 'Accept-Encoding': 'identity'})
    response = _request(request, opener=opener, timeout=timeout, limit=MAX_BYTES, phase='N1 data')
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        saved = freeze_n1_fixture(response, scope=scope, run_key=run_key,
                                   requested_at=requested_at, fetched_at=fetched_at)
        if len(parse_strict_json(saved.records_json)) > max_records:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise PipelineError('Invalid or over-limit N1 response; nothing persisted') from None
    save_atomic(saved, path)
    return _persist(saved, execute_sql, authentication_requests=1, data_requests=1)

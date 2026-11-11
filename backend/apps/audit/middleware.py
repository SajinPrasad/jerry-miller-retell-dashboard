from . import context


class RequestContextMiddleware:
    """Gives every request an id and a clean actor, and echoes the id back.

    Nginx can pass its own `X-Request-ID` (`proxy_set_header X-Request-ID $request_id;`), which
    puts the same id in its access log. The context is cleared on the way out so a Gunicorn
    worker thread never carries one request's actor into the next.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = context.begin(request.headers.get('X-Request-ID', '')[:32])
        try:
            response = self.get_response(request)
            response['X-Request-ID'] = rid
            return response
        finally:
            context.clear()

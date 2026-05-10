import uuid

import structlog

logger = structlog.get_logger(__name__)


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get("HTTP_X_REQUEST_ID", str(uuid.uuid4()))
        request.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = self.get_response(request)

        response["X-Request-ID"] = request_id
        return response


class ScrapyRequestIDMiddleware:
    def process_request(self, request, spider):
        request_id = str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            spider=spider.name if spider else None,
        )
        request.meta["request_id"] = request_id

    def process_response(self, request, response, spider):
        structlog.contextvars.unbind_contextvars("request_id", "spider")
        return response

    def process_exception(self, request, exception, spider):
        structlog.contextvars.unbind_contextvars("request_id", "spider")
        return None

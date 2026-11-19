from ..services import aggregates
class AggregatesController:
    list=staticmethod(aggregates.list_requests)
    submit=staticmethod(aggregates.submit)
    retry=staticmethod(aggregates.retry)

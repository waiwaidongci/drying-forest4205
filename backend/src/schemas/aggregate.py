from pydantic import BaseModel,Field
class AggregateInput(BaseModel):
    source_id:int
    references:list[str]=Field(min_length=1,max_length=1000)
    request_key:str=Field(default='',max_length=120)

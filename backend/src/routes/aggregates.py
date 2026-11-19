from fastapi import APIRouter,Depends
from ..config.database import db_session
from ..schemas.aggregate import AggregateInput
from ..services.auth import current_user,writer
from ..controllers.aggregates import AggregatesController as C
router=APIRouter()
@router.get('/aggregates')
def aggregates(db=Depends(db_session),u=Depends(current_user)):return C.list(db)
@router.post('/aggregates')
def submit(data:AggregateInput,db=Depends(db_session),u=Depends(writer)):return C.submit(db,u,data)
@router.post('/aggregates/{id}/retry')
def retry(id:int,db=Depends(db_session),u=Depends(writer)):return C.retry(db,u,id)

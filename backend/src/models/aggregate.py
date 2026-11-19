from sqlalchemy import Column,Integer,String,Text,JSON,Float,ForeignKey
from ..config.database import Base
from ..config.settings import CONFIG


class AggregateRequest(Base):
    """一份“公开同来源若干试件合计干燥时长”的申请。

    total 在受理时即落库，但只有 state 变为 published 后才会通过 API 返回。
    references 与 record_ids 一一对应且已排序，是线性核验时的系数行；
    受理后不再随来源补录而重算，恢复核验时沿用入库的旧合计。
    """
    __tablename__=CONFIG['aggregate_table']
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    request_key=Column(String(120),unique=True,nullable=False)
    references=Column(JSON,nullable=False)
    record_ids=Column(JSON,nullable=False)
    total=Column(Float,nullable=False)
    state=Column(String(20),default='pending',nullable=False)
    reason=Column(Text,default='')
    requested_by=Column(String(120),nullable=False)
    created_at=Column(String(40),nullable=False)
    decided_at=Column(String(40),default='')


class DisclosureLock(Base):
    """全局核验锁（单行）；乐观版本号保证并发申请只有一方能基于同一版已公开集合落定。"""
    __tablename__='disclosure_locks'
    id=Column(Integer,primary_key=True)
    version=Column(Integer,default=0,nullable=False)

# 合计公开 · 测试说明（gb-0044）

本目录只放测试脚本，不混入源码或页面资源。三个脚本都用临时 SQLite 数据库，
不依赖 PostgreSQL/Redis，可用后端同一虚拟环境直接运行：

```bash
cd backend
python -I ../test-artifacts/gb-0044/test_aggregate_disclosure.py
python -I ../test-artifacts/gb-0044/test_aggregate_concurrency.py
python -I ../test-artifacts/gb-0044/test_aggregate_resume.py
```

## 覆盖范围

- `test_aggregate_disclosure.py`：单件名单拒绝、不同来源编号 422、批内重复编号 422、
  子集/超集相减拒绝、互不相交名单公开、`request_key` 重试只生成一条记录、
  非公开状态不返回合计、补录后恢复沿用旧合计、终态不二次迁移。
- `test_aggregate_concurrency.py`：三份两两相交的合计并发核验，最多两份公开，
  第三份因可线性确定单件被拒绝；重复多轮无竞态错误。
- `test_aggregate_resume.py`：待公开合计落库后模拟进程中断，应用启动时
  `resume_pending` 接着核验；补录改变样本后仍按入库合计判定；同一 pending
  被 8 个线程同时核验时只迁移一次状态。

## 算法说明

已公开合计与本次合计均为“名单内试件时长之和”，构成 0/1 系数矩阵。对系数矩阵
做精确有理数行化简：单位向量 `e_i` 属于行空间 ⇔ `i` 是主元列且该主元行在全部
非主元列为 0。满足即说明单件时长可被线性确定，记入拒绝原因（只点名编号，不泄露数值）。

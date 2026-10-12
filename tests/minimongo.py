"""
A deliberately small MongoDB aggregation interpreter used ONLY by the offline unit tests.

Why it exists: the sandbox this project was developed in has no MongoDB server, and `mongomock` does not
implement $dateFromString, $lookup-with-pipeline, $densify or $setWindowFields. This file implements just the
stages/operators that app/main.py uses, following MongoDB's documented semantics, so the *logic* of the
pipelines (field names, arithmetic, ordering, ranking, gap filling) can be tested without a server.

It is NOT a substitute for MongoDB: scripts/e2e_check.py runs the same checks against a real server.
"""
import datetime as dt
import functools
from typing import Any

MISSING = object()


# --------------------------------------------------------------------------- expressions
def get_path(doc: Any, path: str) -> Any:
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def nullish(v: Any) -> bool:
    return v is MISSING or v is None


def is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def ev(expr: Any, doc: dict, vars_: dict | None = None) -> Any:
    vars_ = vars_ or {}
    if isinstance(expr, str):
        if expr.startswith("$$"):
            name, _, rest = expr[2:].partition(".")
            base = vars_[name]
            return get_path(base, rest) if rest else base
        if expr.startswith("$"):
            return get_path(doc, expr[1:])
        return expr
    if isinstance(expr, list):
        return [ev(e, doc, vars_) for e in expr]
    if isinstance(expr, dict):
        if len(expr) == 1 and next(iter(expr)).startswith("$"):
            return apply_op(next(iter(expr)), expr[next(iter(expr))], doc, vars_)
        return {k: ev(v, doc, vars_) for k, v in expr.items()}
    return expr


def cmp_key(v: Any):
    """BSON-ish total order: null < numbers < strings < dates."""
    if nullish(v):
        return (0, 0)
    if is_num(v):
        return (1, v)
    if isinstance(v, str):
        return (2, v)
    if isinstance(v, dt.datetime):
        return (3, v.replace(tzinfo=None))
    if isinstance(v, bool):
        return (4, v)
    return (5, repr(v))


def apply_op(op: str, args: Any, doc: dict, vars_: dict) -> Any:
    E = lambda x: ev(x, doc, vars_)  # noqa: E731
    if op == "$cond":
        c, a, b = args if isinstance(args, list) else (args["if"], args["then"], args["else"])
        return E(a) if E(c) else E(b)
    if op == "$ifNull":
        v = E(args[0])
        return E(args[1]) if nullish(v) else v
    if op == "$in":
        needle, hay = E(args[0]), E(args[1])
        return needle in hay
    if op == "$and":
        return all(bool(E(a)) for a in args)
    if op == "$or":
        return any(bool(E(a)) for a in args)
    if op in ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte"):
        a, b = E(args[0]), E(args[1])
        ka, kb = cmp_key(a), cmp_key(b)
        return {"$eq": ka == kb, "$ne": ka != kb, "$gt": ka > kb, "$gte": ka >= kb, "$lt": ka < kb, "$lte": ka <= kb}[op]
    if op == "$isNumber":
        return is_num(E(args))
    if op == "$isoDayOfWeek":
        d = E(args)
        return None if nullish(d) else d.isoweekday()
    if op == "$dateFromString":
        s = E(args["dateString"])
        return dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    if op == "$dateToString":
        assert args["format"] == "%Y-%m-%d"
        return E(args["date"]).strftime("%Y-%m-%d")
    if op == "$multiply":
        out = 1
        for a in args:
            v = E(a)
            if nullish(v):
                return None
            out *= v
        return out
    if op == "$add":
        vals = [E(a) for a in args]
        return None if any(nullish(v) for v in vals) else sum(vals)
    if op == "$divide":
        a, b = E(args[0]), E(args[1])
        return None if nullish(a) or nullish(b) else a / b
    if op == "$floor":
        import math
        v = E(args)
        return None if nullish(v) else float(math.floor(v)) if isinstance(v, float) else v
    if op == "$round":
        v, place = E(args[0]), E(args[1])
        assert place == 0
        from decimal import Decimal, ROUND_HALF_EVEN
        return None if nullish(v) else int(Decimal(repr(v)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    if op == "$size":
        return len(E(args))
    if op == "$sum":
        v = E(args)
        if isinstance(v, list):
            return sum(x for x in v if is_num(x))
        return v if is_num(v) else 0
    if op == "$map":
        return [ev(args["in"], doc, {**vars_, args["as"]: item}) for item in E(args["input"])]
    if op == "$filter":
        return [item for item in E(args["input"]) if ev(args["cond"], doc, {**vars_, args["as"]: item})]
    if op == "$concatArrays":
        out: list = []
        for a in args:
            v = E(a)
            if nullish(v):
                return None
            out += v
        return out
    raise NotImplementedError(op)


# --------------------------------------------------------------------------- $match
def match_doc(doc: dict, query: dict) -> bool:
    for field, cond in query.items():
        value = get_path(doc, field)
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if nullish(value) and op in ("$gt", "$gte", "$lt", "$lte"):
                    return False
                a, b = cmp_key(value), cmp_key(operand)
                ok = {"$gt": a > b, "$gte": a >= b, "$lt": a < b, "$lte": a <= b, "$eq": a == b, "$ne": a != b}[op]
                if not ok:
                    return False
        else:
            if cmp_key(value) != cmp_key(cond):
                return False
    return True


# --------------------------------------------------------------------------- stages
def run_pipeline(docs: list[dict], pipeline: list[dict], database) -> list[dict]:
    for stage in pipeline:
        (name, spec), = stage.items()
        docs = STAGES[name](docs, spec, database)
    return docs


def st_match(docs, spec, _):
    return [d for d in docs if match_doc(d, spec)]


def st_group(docs, spec, _):
    groups: dict = {}
    order: list = []
    for d in docs:
        key = ev(spec["_id"], d)
        k = repr(key)
        if k not in groups:
            groups[k] = {"_id": key, "_rows": []}
            order.append(k)
        groups[k]["_rows"].append(d)
    out = []
    for k in order:
        g = groups[k]
        row = {"_id": g["_id"]}
        for field, acc in spec.items():
            if field == "_id":
                continue
            (op, expr), = acc.items()
            assert op == "$sum", op
            row[field] = sum(v for v in (ev(expr, d) for d in g["_rows"]) if is_num(v))
        out.append(row)
    return out


def st_lookup(docs, spec, database):
    out = []
    for d in docs:
        local = get_path(d, spec["localField"])
        foreign = [f for f in database[spec["from"]].find({}) if cmp_key(get_path(f, spec["foreignField"])) == cmp_key(local)]
        if "pipeline" in spec:
            foreign = run_pipeline(foreign, spec["pipeline"], database)
        out.append({**d, spec["as"]: foreign})
    return out


def st_project(docs, spec, _):
    out = []
    include_id = spec.get("_id", 1) not in (0, False)
    for d in docs:
        row = {}
        if include_id and "_id" in d:
            row["_id"] = d["_id"]
        for field, how in spec.items():
            if field == "_id":
                if how not in (0, 1, True, False):
                    row["_id"] = ev(how, d)
                continue
            if how in (1, True):
                v = get_path(d, field)
                if v is not MISSING:
                    row[field] = v
            elif how in (0, False):
                raise NotImplementedError("exclusion projection")
            else:
                row[field] = ev(how, d)
        out.append(row)
    return out


def st_addfields(docs, spec, _):
    return [{**d, **{k: ev(v, d) for k, v in spec.items()}} for d in docs]


def st_unwind(docs, spec, _):
    path = spec[1:]
    out = []
    for d in docs:
        for item in d.get(path) or []:
            out.append({**d, path: item})
    return out


def st_replace_root(docs, spec, _):
    return [ev(spec["newRoot"], d) for d in docs]


def sort_docs(docs, spec):
    def compare(a, b):
        for field, direction in spec.items():
            ka, kb = cmp_key(get_path(a, field)), cmp_key(get_path(b, field))
            if ka != kb:
                return (-1 if ka < kb else 1) * direction
        return 0
    return sorted(docs, key=functools.cmp_to_key(compare))


def st_sort(docs, spec, _):
    return sort_docs(docs, spec)


def st_densify(docs, spec, _):
    field = spec["field"]
    r = spec["range"]
    assert r["step"] == 1 and r["unit"] == "day"
    lo, hi = r["bounds"]
    present = {d[field] for d in docs if field in d}
    out = list(docs)
    cur = lo
    while cur < hi:  # upper bound exclusive, as documented
        if cur not in present:
            out.append({field: cur})
        cur += dt.timedelta(days=1)
    return sort_docs(out, {field: 1})


def st_set_window_fields(docs, spec, _):
    docs = sort_docs(docs, spec["sortBy"])
    (sort_field, _dir), = spec["sortBy"].items()
    out = [dict(d) for d in docs]
    for name, acc in spec["output"].items():
        (op, expr) = next((k, v) for k, v in acc.items() if k != "window")
        if op == "$rank":
            keys = [cmp_key(get_path(d, sort_field)) for d in docs]
            for i, d in enumerate(out):
                d[name] = 1 + sum(1 for j in range(len(docs)) if keys[j] != keys[i] and
                                  (keys[j] > keys[i] if _dir == -1 else keys[j] < keys[i]))
        elif op == "$sum":
            lo, hi = acc["window"]["documents"]
            vals = [ev(expr, d) for d in docs]
            for i, d in enumerate(out):
                a = 0 if lo == "unbounded" else max(0, i + lo)
                b = len(docs) - 1 if hi == "unbounded" else (i if hi == "current" else min(len(docs) - 1, i + hi))
                d[name] = sum(v for v in vals[a:b + 1] if is_num(v))
        else:
            raise NotImplementedError(op)
    # window outputs are visible to later stages only; keep the originals untouched for this stage
    return out


STAGES = {
    "$match": st_match, "$group": st_group, "$lookup": st_lookup, "$project": st_project,
    "$addFields": st_addfields, "$unwind": st_unwind, "$replaceRoot": st_replace_root, "$sort": st_sort,
    "$densify": st_densify, "$setWindowFields": st_set_window_fields,
}

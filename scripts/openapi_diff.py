"""
OpenAPI contract diff — stdlib only.

    python scripts/openapi_diff.py before.json after.json [--intentional intentional.json] [--markdown out.md]

Classifies every difference between two OpenAPI documents as

    ADDITIVE                new path / method / response field / optional
                            request field / new enum value / new header
    INTENTIONAL BREAKING    a removal, rename, type change or requiredness
                            change that is listed in the intentional file
                            (each with a reason) — reported, never hidden
    UNINTENTIONAL BREAKING  every other breaking change — the gate fails
                            (exit 1) when at least one exists

The intentional file is a JSON list of {"match": "<substring of the change
key>", "reason": "..."}; a change is intentional when its key contains the
substring. Change keys look like:

    path:/api/x                       (removed path)
    op:GET /api/x                     (removed operation)
    param:GET /api/x:page             (removed/changed query/path parameter)
    schema:Foo.bar                    (removed/changed schema property)
    schema:Foo.bar:required           (property became required)
    schema:Foo.bar:type               (property type changed)
    schema:Foo:enum                   (enum value removed)
    schema:Foo                        (schema removed)
    request:POST /api/x               (request body schema changed/removed)
    response:GET /api/x:200           (response removed / schema ref changed)
"""
import argparse
import json
import sys

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _schemas(doc):
    return (doc.get("components") or {}).get("schemas") or {}


def _props(schema):
    return (schema or {}).get("properties") or {}


def _type_of(prop):
    if not isinstance(prop, dict):
        return None
    if "$ref" in prop:
        return prop["$ref"]
    if "anyOf" in prop:
        return "anyOf:" + "|".join(sorted(json.dumps(x, sort_keys=True) for x in prop["anyOf"]))
    if "allOf" in prop:
        return "allOf:" + "|".join(sorted(json.dumps(x, sort_keys=True) for x in prop["allOf"]))
    t = prop.get("type")
    if t == "array":
        return f"array<{_type_of(prop.get('items'))}>"
    return t


def _nullable(prop):
    if not isinstance(prop, dict):
        return False
    if "anyOf" in prop:
        return any(x.get("type") == "null" for x in prop["anyOf"] if isinstance(x, dict))
    return bool(prop.get("nullable"))


def diff(before, after):
    additive, breaking = [], []

    # ---- paths / operations
    bp, ap = before.get("paths") or {}, after.get("paths") or {}
    for path in sorted(set(bp) | set(ap)):
        if path not in ap:
            breaking.append((f"path:{path}", "path removed"))
            continue
        if path not in bp:
            additive.append((f"path:{path}", "path added"))
            continue
        for m in HTTP_METHODS:
            bo, ao = bp[path].get(m), ap[path].get(m)
            key = f"op:{m.upper()} {path}"
            if bo and not ao:
                breaking.append((key, "operation removed"))
                continue
            if ao and not bo:
                additive.append((key, "operation added"))
                continue
            if not (bo and ao):
                continue
            # parameters
            bpar = {(p.get("in"), p.get("name")): p for p in bo.get("parameters") or []}
            apar = {(p.get("in"), p.get("name")): p for p in ao.get("parameters") or []}
            for k in sorted(set(bpar) | set(apar), key=str):
                pkey = f"param:{m.upper()} {path}:{k[1]}"
                if k not in apar:
                    breaking.append((pkey, f"{k[0]} parameter removed"))
                elif k not in bpar:
                    if apar[k].get("required"):
                        breaking.append((pkey, f"new REQUIRED {k[0]} parameter"))
                    else:
                        additive.append((pkey, f"optional {k[0]} parameter added"))
                else:
                    if not bpar[k].get("required") and apar[k].get("required"):
                        breaking.append((pkey, "parameter became required"))
                    bt, at = _type_of(bpar[k].get("schema")), _type_of(apar[k].get("schema"))
                    if bt != at:
                        breaking.append((pkey + ":type", f"parameter type {bt} → {at}"))
            # request body
            brb = json.dumps((bo.get("requestBody") or {}).get("content"), sort_keys=True)
            arb = json.dumps((ao.get("requestBody") or {}).get("content"), sort_keys=True)
            if brb != arb:
                if bo.get("requestBody") and not ao.get("requestBody"):
                    breaking.append((f"request:{m.upper()} {path}", "request body removed"))
                elif not bo.get("requestBody") and ao.get("requestBody"):
                    req = (ao["requestBody"] or {}).get("required")
                    (breaking if req else additive).append((f"request:{m.upper()} {path}", "request body added"))
                else:
                    breaking.append((f"request:{m.upper()} {path}", "request body content changed"))
            # responses
            bres, ares = bo.get("responses") or {}, ao.get("responses") or {}
            for code in sorted(set(bres) | set(ares)):
                rkey = f"response:{m.upper()} {path}:{code}"
                if code not in ares:
                    breaking.append((rkey, "response removed"))
                elif code not in bres:
                    additive.append((rkey, "response added"))
                else:
                    bs = json.dumps(((bres[code].get("content") or {}).get("application/json") or {}).get("schema"), sort_keys=True)
                    as_ = json.dumps(((ares[code].get("content") or {}).get("application/json") or {}).get("schema"), sort_keys=True)
                    if bs != as_:
                        breaking.append((rkey, "response schema reference changed"))
                    bh, ah = set(bres[code].get("headers") or {}), set(ares[code].get("headers") or {})
                    for h in sorted(ah - bh):
                        additive.append((rkey + f":header:{h}", "response header added"))
                    for h in sorted(bh - ah):
                        breaking.append((rkey + f":header:{h}", "response header removed"))

    # ---- schemas
    bs, as_ = _schemas(before), _schemas(after)
    for name in sorted(set(bs) | set(as_)):
        if name not in as_:
            breaking.append((f"schema:{name}", "schema removed"))
            continue
        if name not in bs:
            additive.append((f"schema:{name}", "schema added"))
            continue
        b, a = bs[name], as_[name]
        bprops, aprops = _props(b), _props(a)
        breq, areq = set(b.get("required") or []), set(a.get("required") or [])
        for prop in sorted(set(bprops) | set(aprops)):
            key = f"schema:{name}.{prop}"
            if prop not in aprops:
                breaking.append((key, "property removed"))
            elif prop not in bprops:
                if prop in areq and not _nullable(aprops[prop]):
                    # new required property: breaking for REQUEST schemas, additive for responses;
                    # we cannot tell here, so report as breaking-candidate with a hint
                    breaking.append((key + ":required", "new REQUIRED property"))
                else:
                    additive.append((key, "property added"))
            else:
                bt, at = _type_of(bprops[prop]), _type_of(aprops[prop])
                if bt != at:
                    if _nullable(aprops[prop]) and not _nullable(bprops[prop]) and \
                            (bt in (at or "") or (at or "").startswith("anyOf")):
                        additive.append((key + ":type", f"became nullable ({bt} → {at})"))
                    else:
                        breaking.append((key + ":type", f"type {bt} → {at}"))
                if prop in areq and prop not in breq:
                    breaking.append((key + ":required", "property became required"))
                benum, aenum = set(bprops[prop].get("enum") or []), set(aprops[prop].get("enum") or [])
                if benum - aenum:
                    breaking.append((key + ":enum", f"enum values removed: {sorted(benum - aenum)}"))
                if aenum - benum:
                    additive.append((key + ":enum", f"enum values added: {sorted(aenum - benum)}"))
        benum, aenum = set(b.get("enum") or []), set(a.get("enum") or [])
        if benum - aenum:
            breaking.append((f"schema:{name}:enum", f"enum values removed: {sorted(benum - aenum)}"))
        if aenum - benum:
            additive.append((f"schema:{name}:enum", f"enum values added: {sorted(aenum - benum)}"))
    return additive, breaking


def classify(breaking, intentional):
    intended, unintended = [], []
    for key, what in breaking:
        reason = next((i["reason"] for i in intentional if i["match"] in key), None)
        (intended if reason else unintended).append((key, what, reason))
    return intended, unintended


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--intentional", help="JSON list of {match, reason}")
    ap.add_argument("--markdown", help="write the report here")
    args = ap.parse_args()
    before, after = _load(args.before), _load(args.after)
    intentional = _load(args.intentional) if args.intentional else []
    additive, breaking = diff(before, after)
    intended, unintended = classify(breaking, intentional)

    lines = ["# OpenAPI contract diff", "",
             f"before: {len(before.get('paths') or {})} paths / {len(_schemas(before))} schemas — "
             f"after: {len(after.get('paths') or {})} paths / {len(_schemas(after))} schemas", "",
             f"ADDITIVE: {len(additive)} · INTENTIONAL BREAKING: {len(intended)} · UNINTENTIONAL BREAKING: {len(unintended)}", ""]
    lines += ["## ADDITIVE", ""] + [f"- `{k}` — {w}" for k, w in additive] + [""]
    lines += ["## INTENTIONAL BREAKING (each with its reason)", ""] + [f"- `{k}` — {w} — **{r}**" for k, w, r in intended] + [""]
    lines += ["## UNINTENTIONAL BREAKING (must be empty)", ""] + ([f"- `{k}` — {w}" for k, w, _ in unintended] or ["- none"]) + [""]
    report = "\n".join(lines)
    print(report)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(report)
    sys.exit(1 if unintended else 0)


if __name__ == "__main__":
    main()

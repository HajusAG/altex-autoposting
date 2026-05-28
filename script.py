"""Altex.ro autoposting — production script for Coolify.

Loops over Odoo x_auto_posting tasks (website=194 HAJUS AG, stage="New task"),
fetches product + AWS images, AI-picks attribute_set/category/attrs via
OpenRouter, signs and POSTs to Altex.ro marketplace, verifies via GET,
updates the Odoo task stage.

Env vars (Coolify):
  ALTEX_BASE, ALTEX_PUB, ALTEX_PRIV
  ODOO_URL, ODOO_DB, ODOO_UID, ODOO_API_KEY
  OPENROUTER_API_KEY, OPENROUTER_MODEL (default deepseek/deepseek-chat)
  N8N_AWS_START_URL, N8N_AWS_RESULT_URL
  ALTEX_PENDING_LIMIT (optional, default 0=all)
  ALTEX_PENDING_WEBSITE_ID (default 194)
  ALTEX_TASK_STAGE_NEW (default 1)
  ALTEX_TASK_STAGE_PENDING (default 6)
  ALTEX_PRICELIST_RON (default 21)
"""
import hashlib
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import xmlrpc.client
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"missing env var {name}")
    return v


# ---- Altex ----
BASE = env("ALTEX_BASE", "https://marketplace.altex.ro")
PUB = env("ALTEX_PUB", required=True)
PRIV = env("ALTEX_PRIV", required=True)

# ---- Odoo ----
ODOO_URL = env("ODOO_URL", "https://odoo.boni.tools")
ODOO_DB = env("ODOO_DB", required=True)
ODOO_UID = int(env("ODOO_UID", "2"))
ODOO_KEY = env("ODOO_API_KEY", required=True)

# ---- OpenRouter ----
OR_KEY = env("OPENROUTER_API_KEY", required=True)
OR_MODEL = env("OPENROUTER_MODEL", "deepseek/deepseek-chat")

# ---- n8n AWS Images ----
N8N_AWS_START = env("N8N_AWS_START_URL", "https://n8np2.boni.tools/webhook/get-aws-images-start")
N8N_AWS_RESULT = env("N8N_AWS_RESULT_URL", "https://n8np2.boni.tools/webhook/get-aws-images-result")

# ---- Tunables ----
WEBSITE_ID = int(env("ALTEX_PENDING_WEBSITE_ID", "194"))
STAGE_NEW = int(env("ALTEX_TASK_STAGE_NEW", "1"))
STAGE_PENDING = int(env("ALTEX_TASK_STAGE_PENDING", "6"))
PRICELIST_RON = int(env("ALTEX_PRICELIST_RON", "21"))
LIMIT = int(env("ALTEX_PENDING_LIMIT", "0"))

HERE = os.path.dirname(os.path.abspath(__file__))

BONI_SHOP_BRANDS = {
    "BRYZA", "Cepex", "Cellfast", "Sobime", "Cell Fast", "Hase Safety Gloves GmbH",
    "Rain Bird", "FIXAFLEX", "Geli GmbH", "KWB", "Beckmann&Brehm", "Zill", "Cordes",
    "Cook King", "nmc Deutschland GmbH", "NMC", "COYOTTO GARDEN POTS",
    "Brandenburg Späne", "Pipetec GmbH", "Happy People GmbH & Co. KG",
    "BB-Verpackungen GmbH", "CORDES GmbH & Co.KG", "Dresselhaus", "ecoon",
    "Boni Brands", "boni-shop.com", "BGS", "Rasenspecht",
    "Quedlinburger Saatgut GmbH", "Quedlinburger", "Naturen", "Celaflor", "Tesa",
    "Kurt Europe", "TFP Sp. z o.o.", "Schumacher PL", "Netbox", "Mayer Network GmbH",
    "Substral", "DJUX", "Karl Verpackungen", "KM",
}


# ==================== Altex signing ====================

def rfc3986(s):
    return urllib.parse.quote(str(s), safe="")


def php_build_query_pipe(obj):
    pairs = []

    def walk(prefix, val):
        if val is None:
            pairs.append(f"{rfc3986(prefix)}=")
        elif isinstance(val, bool):
            pairs.append(f"{rfc3986(prefix)}={rfc3986('1' if val else '')}")
        elif isinstance(val, (list, tuple)):
            for i, v in enumerate(val):
                walk(f"{prefix}[{i}]", v)
        elif isinstance(val, dict):
            for k, v in val.items():
                walk(f"{prefix}[{k}]", v)
        else:
            pairs.append(f"{rfc3986(prefix)}={rfc3986(val)}")

    if isinstance(obj, dict):
        for k, v in obj.items():
            walk(k, v)
    return "|".join(pairs)


def altex_sign(method, url, body=None):
    sha_priv = hashlib.sha512(PRIV.encode()).hexdigest()
    encoded = ""
    if method in ("GET", "DELETE"):
        if "?" in url:
            q = url.split("?", 1)[1]
            parts = q.split("&") if q else []
            re_pairs = []
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    re_pairs.append(
                        f"{rfc3986(urllib.parse.unquote(k))}={rfc3986(urllib.parse.unquote(v))}"
                    )
                else:
                    re_pairs.append(f"{rfc3986(urllib.parse.unquote(p))}=")
            encoded = "|".join(re_pairs)
    elif method in ("POST", "PUT"):
        if body and isinstance(body, dict):
            encoded = php_build_query_pipe(body)
    now = datetime.now(timezone.utc)
    date_code = f"{now.day:02d}{now.month:02d}"
    pre = f"{PUB}||{sha_priv}||{encoded}||{date_code}"
    h = hashlib.sha512(pre.encode()).hexdigest().lower()
    return date_code + h


def altex_req(method, path, params=None, body=None, timeout=60):
    qs = ""
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params)
    url = f"{BASE}{path}" + (f"?{qs}" if qs else "")
    headers = {
        "X-Request-Public-Key": PUB,
        "X-Request-Signature": altex_sign(method, url, body),
        "Accept-Encoding": "identity",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

    def _decode_raw(hdrs, raw):
        ce = (hdrs.get("Content-Encoding") or "").lower()
        if ce == "gzip":
            import gzip as _gz
            try: raw = _gz.decompress(raw)
            except Exception: pass
        elif ce == "deflate":
            import zlib as _zl
            try: raw = _zl.decompress(raw)
            except Exception: pass
        return raw

    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = _decode_raw(resp.headers, resp.read())
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = _decode_raw(e.headers, e.read())
        text = raw.decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, {"error_body": text}


# ==================== Odoo ====================

def odoo_call(model, method, args, kwargs=None):
    s = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return s.execute_kw(ODOO_DB, ODOO_UID, ODOO_KEY, model, method, args, kwargs or {})


# ==================== OpenRouter LLM ====================

def llm(messages, max_tokens=1500, temperature=0.0, response_format=None):
    payload = {
        "model": OR_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OR_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://altex-autoposting.local",
            "X-Title": "Altex Autoposting",
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt == 2:
                raise
            print(f"  LLM retry {attempt + 1}: {e}", flush=True)
            time.sleep(5 * (attempt + 1))


# ==================== AWS Images via n8n ====================

def aws_images_for(product_id, timeout=180):
    print(f"  AWS Images start product_id={product_id}", flush=True)
    r = urllib.request.urlopen(f"{N8N_AWS_START}?product_id={product_id}", timeout=30)
    job = json.loads(r.read())
    job_id = job["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        r = urllib.request.urlopen(f"{N8N_AWS_RESULT}?job_id={job_id}", timeout=30)
        d = json.loads(r.read())
        if d.get("status") == "done":
            return d.get("result", {})
        if d.get("status") == "error":
            raise RuntimeError(f"AWS Images error: {d}")
    raise TimeoutError("AWS Images timed out")


# ==================== Helpers ====================

def normalize_brand(sku, raw_brand):
    if not raw_brand:
        return ""
    if sku and sku.startswith("WF-"):
        return "Westfalia"
    if raw_brand in BONI_SHOP_BRANDS:
        return "Boni-Shop"
    return raw_brand


def load_json(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return json.load(f)


# ==================== Per-task processing ====================

def post_one_task(task, allowed_list, allowed_map, all_sets):
    task_id = task["id"]
    product_id = task["x_studio_product"][0] if task.get("x_studio_product") else None
    if not product_id:
        return False, "no_product_on_task"

    prod = odoo_call(
        "product.template", "read",
        [[product_id]],
        {"fields": ["id", "default_code", "name", "barcode", "list_price",
                    "qty_available", "x_studio_description_short",
                    "website_description", "x_studio_brand_name",
                    "x_studio_manufacturer_name", "x_studio_attributes",
                    "public_categ_ids", "country_of_origin", "taxes_id"]},
    )[0]

    sku = prod.get("default_code") or ""
    if sku.startswith("BONI-"):
        sku = sku[len("BONI-"):]

    raw_brand = prod.get("x_studio_manufacturer_name") or prod.get("x_studio_brand_name") or ""
    brand = normalize_brand(sku, raw_brand)
    ean = prod.get("barcode") or task.get("x_studio_ean_barcode") or ""
    if not ean:
        return False, "no_ean"

    title = prod.get("name") or ""
    short = prod.get("x_studio_description_short") or ""
    full = prod.get("website_description") or ""
    description = full if (full and len(full) <= 8000 and len(full) > 50) else short
    if not description or len(description) < 50:
        description = short or full or title

    # RON price
    items = odoo_call(
        "product.pricelist.item", "search_read",
        [[["pricelist_id", "=", PRICELIST_RON], ["product_tmpl_id", "=", product_id]]],
        {"fields": ["fixed_price"]},
    )
    if not items:
        items = odoo_call(
            "product.pricelist.item", "search_read",
            [[["pricelist_id", "=", PRICELIST_RON], ["product_id.product_tmpl_id", "=", product_id]]],
            {"fields": ["fixed_price"]},
        )
    if not items:
        return False, "no_ron_price"
    price = float(items[0]["fixed_price"])

    tax_amount = 19
    if prod.get("taxes_id"):
        taxes = odoo_call("account.tax", "read", [prod["taxes_id"]], {"fields": ["amount"]})
        tax_amount = max(t["amount"] for t in taxes) if taxes else 19
    vat = 21 if tax_amount == 19 else (11 if tax_amount < 19 else int(tax_amount))

    stock = int(round(prod.get("qty_available") or 0))

    print(f"  sku={sku} ean={ean} price={price} stock={stock} vat={vat} brand={brand!r}", flush=True)

    # AWS images
    img = aws_images_for(product_id)
    main_url = img.get("main_image_url")
    add_urls = img.get("additional_images_urls") or []
    if not main_url:
        return False, "no_main_image"
    image_urls = ([main_url] + add_urls)[:2]

    # Pick attribute_set
    allowed_set_ids = {int(a["attributeId"]) for a in allowed_list}
    sets_by_id = {s["id"]: s for s in all_sets}
    candidate_sets = [
        {"id": aid, "name": sets_by_id[aid]["name"]}
        for aid in sorted(allowed_set_ids)
        if aid in sets_by_id and str(aid) in allowed_map
    ]
    kw_tokens = {t.strip(".,()/-\"'") for t in title.lower().split() if len(t.strip(".,()/-\"'")) >= 4}
    narrowed = [s for s in candidate_sets if any(k in s["name"].lower() for k in kw_tokens)]
    if len(narrowed) < 10:
        narrowed = candidate_sets

    set_prompt = f"""You MUST pick exactly ONE attribute_set from the list.
Picking nothing is NOT an option — choose the best fit even if imperfect.

Strict JSON output (no other keys):
{{"id": <int>, "name": "<name>"}}

Product:
- Title: {title}
- Description: {description[:500]}
- Brand: {brand}

Attribute sets (id, name):
{json.dumps(narrowed, ensure_ascii=False)}
"""
    raw = llm([{"role": "user", "content": set_prompt}], max_tokens=200,
              response_format={"type": "json_object"})
    set_pick = json.loads(raw)
    set_id = int(set_pick.get("id") or 0)
    if not set_id or set_id not in allowed_set_ids:
        set_id = narrowed[0]["id"]

    # Build ordered list of (set_id, path) candidates for retry on 403
    set_order = [set_id] + [s["id"] for s in narrowed if s["id"] != set_id]
    set_path_queue = []
    for sid in set_order:
        paths = allowed_map.get(str(sid)) or []
        if not paths:
            continue
        if len(paths) == 1:
            set_path_queue.append((sid, paths[0]))
            continue
        cat_prompt = f"""You are classifying products into Altex.ro marketplace categories.

Product:
- Title: {title}
- Description: {description[:500]}
- Brand: {brand}

Pick exactly ONE category from the provided list. Always pick something.

Strict JSON:
{{"codes": [<int>,...], "path": "<path>"}}

Categories:
{json.dumps(paths, ensure_ascii=False)}
"""
        try:
            raw = llm([{"role": "user", "content": cat_prompt}], max_tokens=300,
                      response_format={"type": "json_object"})
            picked = json.loads(raw)
            valid = next((p for p in paths if p["codes"] == picked.get("codes")), None)
            ordered = ([valid] if valid else []) + [p for p in paths if not valid or p["codes"] != valid["codes"]]
        except Exception:
            ordered = list(paths)
        for p in ordered:
            set_path_queue.append((sid, p))
        if len(set_path_queue) >= 8:
            break

    if not set_path_queue:
        return False, f"no_paths_for_set_{set_id}"

    attrs_cache = {}
    last_status = None
    last_resp = None
    for attempt_idx, (set_id, chosen_cat) in enumerate(set_path_queue, 1):
        print(f"  attempt {attempt_idx}/{len(set_path_queue)} set={set_id} cat={chosen_cat.get('path')}", flush=True)

        # Attributes for set (cached)
        if set_id in attrs_cache:
            all_attrs = attrs_cache[set_id]
        else:
            all_attrs = []
            page = 1
            while True:
                params = [("items_per_page", 100), ("page_nr", page)]
                s, r = altex_req("GET", f"/v2.0/catalog/sets/{set_id}/attributes", params=params)
                if s != 200:
                    break
                items_a = (r.get("data") or {}).get("items") or []
                all_attrs.extend(items_a)
                meta = (r.get("data") or {}).get("meta") or {}
                total = meta.get("total") or len(items_a)
                if len(items_a) < 100 or len(all_attrs) >= total:
                    break
                page += 1
            attrs_cache[set_id] = all_attrs

        required = []
        brand_attr = None
        for a in all_attrs:
            req_sets = a.get("required_attribute_sets") or []
            if isinstance(req_sets, dict):
                req_sets = list(req_sets.values())
            is_req = set_id in [int(x) for x in req_sets]
            if a.get("code") == "brand":
                if is_req:
                    brand_attr = a
                continue
            if is_req:
                required.append({
                    "code": a["code"], "name": a.get("name"),
                    "value_type": a.get("value_type"),
                    "attribute_values": a.get("attribute_values"),
                })

        found_brand_id = None
        if brand_attr and brand:
            bl = brand.replace("®", "").lower().strip()
            for v in brand_attr.get("attribute_values") or []:
                if v.get("name", "").replace("®", "").lower().strip() == bl:
                    found_brand_id = v["id"]
                    break

        attributes_obj = {}
        if required:
            attr_prompt = f"""Map product info to required Altex marketplace attributes.

Return STRICT JSON: an array of objects:
[{{"code":"...","values":["..."]}}]

Rules:
- For select/multiselect: values = array of attribute_value.id (as STRINGS)
- For text/number/decimal: values = [single string]
- For boolean: values = ["0"] or ["1"]
- ALWAYS fill every attribute; pick the most generic value if uncertain
- Do not include the "brand" code

PRODUCT:
- Title: {title}
- Description: {description[:600]}
- Brand: {brand}
- Country of origin: {(prod.get("country_of_origin") or [None, ""])[1]}

ATTRIBUTES:
{json.dumps(required, ensure_ascii=False)[:30000]}
"""
            raw = llm([{"role": "user", "content": attr_prompt}], max_tokens=3000,
                      response_format={"type": "json_object"})
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "output" in parsed:
                    parsed = parsed["output"]
                if isinstance(parsed, dict) and "attributes" in parsed:
                    parsed = parsed["attributes"]
                if not isinstance(parsed, list):
                    for v in (parsed.values() if isinstance(parsed, dict) else []):
                        if isinstance(v, list):
                            parsed = v
                            break
            except Exception:
                parsed = []

            type_map = {a["code"]: a.get("value_type") for a in required}
            for it in parsed:
                code = it.get("code")
                vals = it.get("values") or []
                if not code or not vals:
                    continue
                vt = type_map.get(code)
                if vt == "multiselect":
                    try:
                        attributes_obj[code] = [int(x) for x in vals]
                    except Exception:
                        attributes_obj[code] = [str(x) for x in vals]
                else:
                    v0 = vals[0]
                    try:
                        attributes_obj[code] = int(v0)
                    except Exception:
                        try:
                            attributes_obj[code] = float(v0)
                        except Exception:
                            attributes_obj[code] = str(v0)

        if found_brand_id:
            attributes_obj["brand"] = int(found_brand_id)

        images = {}
        for i, u in enumerate(image_urls):
            images["main" if i == 0 else str(i - 1)] = u

        offer = {
            "seller_product_code": sku,
            "status": 1,
            "price": price,
            "min_selling_price": price,
            "vat": vat,
            "min_delivery_interval": 5,
            "max_delivery_interval": 5,
            "stock": stock,
        }
        product_payload = {
            "attribute_set_id": int(set_id),
            "category_ids": [int(chosen_cat["codes"][-1])],
            "ean": str(ean),
            "name": title,
            "description": description,
            "attributes": attributes_obj,
            "images": images,
            "offer": offer,
        }
        products = {"0": product_payload}

        status, resp = altex_req("POST", "/v2.0/catalog/product/", body=products, timeout=120)
        last_status, last_resp = status, resp
        print(f"  POST HTTP {status}: {json.dumps(resp, ensure_ascii=False)[:400]}", flush=True)

        if status >= 400:
            err_text = json.dumps(resp, ensure_ascii=False).lower()
            if "already exists" in err_text and "ean" in err_text:
                print("  EAN already on Altex — treating as success", flush=True)
            elif status == 403 and "no allowed categories" in err_text:
                print("  category rejected by Altex — trying next candidate", flush=True)
                continue
            else:
                return False, f"http_{status}"

        # Verify
        time.sleep(3)
        status2, resp2 = altex_req("GET", "/v2.0/catalog/product", params=[("ean", ean)])
        items_v = (resp2.get("data") or {}).get("items") or []
        found = next(
            (i for i in items_v
             if i.get("ean") and ean in (i["ean"] if isinstance(i["ean"], list) else [i["ean"]])),
            None,
        )
        if not found:
            return False, "verify_not_found"
        print(f"  VERIFIED altex_id={found.get('id')} approval={found.get('approval_status')}", flush=True)
        return True, found.get("id")

    return False, f"http_{last_status}_all_categories_rejected"


# ==================== Main ====================

def main():
    print(f"=== Altex autoposting run @ {datetime.now(timezone.utc).isoformat()} ===", flush=True)
    print(f"website_id={WEBSITE_ID} stage_new={STAGE_NEW} -> stage_pending={STAGE_PENDING}", flush=True)

    allowed_list = load_json("_allowed_categories.json")
    allowed_map = load_json("_allowed_map.json")
    all_sets = load_json("_altex_sets.json")
    print(f"  loaded: {len(allowed_list)} allowed_categories, "
          f"{len(allowed_map)} set->paths, {len(all_sets)} sets", flush=True)

    domain = [
        ["x_studio_website", "=", WEBSITE_ID],
        ["x_studio_stage_id", "=", STAGE_NEW],
    ]
    kwargs = {
        "fields": ["id", "x_studio_product", "x_studio_ean_barcode",
                   "x_studio_stage_id", "x_studio_website"],
        "order": "id asc",
    }
    if LIMIT > 0:
        kwargs["limit"] = LIMIT
    tasks = odoo_call("x_auto_posting", "search_read", [domain], kwargs)
    print(f"  pending tasks: {len(tasks)}", flush=True)

    ok_count = 0
    fail_count = 0
    errors_by_reason = {}
    for idx, t in enumerate(tasks, 1):
        print(f"\n[{idx}/{len(tasks)}] task {t['id']}", flush=True)
        try:
            ok, info = post_one_task(t, allowed_list, allowed_map, all_sets)
        except Exception as e:
            ok, info = False, f"exc:{type(e).__name__}:{e}"
            print(f"  EXC: {info}", flush=True)

        if ok:
            ok_count += 1
            try:
                vals = {"x_studio_stage_id": STAGE_PENDING}
                if info and isinstance(info, str) and not info.startswith("exc:"):
                    vals["x_studio_listing_id"] = info
                odoo_call("x_auto_posting", "write", [[t["id"]], vals])
                print(f"  Odoo task {t['id']} -> stage {STAGE_PENDING} listing_id={info}", flush=True)
            except Exception as e:
                print(f"  Odoo update failed: {e}", flush=True)
        else:
            fail_count += 1
            errors_by_reason[str(info)] = errors_by_reason.get(str(info), 0) + 1

        time.sleep(1)

    print(f"\n=== DONE: ok={ok_count} fail={fail_count} ===", flush=True)
    if errors_by_reason:
        print("Errors breakdown:", flush=True)
        for reason, n in sorted(errors_by_reason.items(), key=lambda x: -x[1]):
            print(f"  {n:4d}  {reason}", flush=True)


if __name__ == "__main__":
    main()

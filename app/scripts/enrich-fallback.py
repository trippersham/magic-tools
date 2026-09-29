# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""Resilient enrichment that bypasses prep.mjs's lake-image path (our lake has no
image_normal column). Metadata + art_crop come from one lake read; the full card
image comes from the 0.7.0 scryfall_cache façade per card, falling back to the
lake art_crop when the façade misses. Writes src/data/enriched.json in the exact
shape the Astro app expects."""
import duckdb, json, os, subprocess

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(APP, "src", "data")
LAKE = os.path.expanduser("~/.local/share/make-magic/make_magic.duckdb")
FACADE = os.path.expanduser("~/.claude/plugins/cache/magic-tools/make-magic/0.7.0/scripts/scryfall_cache.py")

deck = json.load(open(os.path.join(DATA, "deck.json")))
cs = json.load(open(os.path.join(DATA, "changeset.json")))

names = {c["name"] for c in deck}
names |= {a["name"] for a in cs["adds"]}
names |= {c["name"] for c in cs["considerations"]}

con = duckdb.connect(LAKE, read_only=True)
lake = {}
for n in names:
    r = con.execute(
        "select name,cmc,mana_cost,type_line,colors,art_crop,scryfall_uri "
        "from oracle_cards where lower(name)=lower(?) limit 1", [n]).fetchone()
    if r:
        colors = json.loads(r[4]) if isinstance(r[4], str) else (r[4] or [])
        lake[n] = dict(name=r[0], cmc=r[1], mana_cost=r[2], type_line=r[3],
                       colors=colors, art_crop=r[5], scryfall_uri=r[6])

def facade_image(name):
    try:
        out = subprocess.run(["uv", "run", "--script", FACADE, "get-card", name],
                             capture_output=True, text=True, timeout=30).stdout
        c = json.loads(out)
        iu = c.get("image_uris") or {}
        if iu.get("normal"): return iu["normal"], c.get("prices", {}).get("usd")
        faces = c.get("card_faces") or []
        if faces and faces[0].get("image_uris", {}).get("normal"):
            return faces[0]["image_uris"]["normal"], c.get("prices", {}).get("usd")
    except Exception:
        pass
    return None, None

def enrich(name, extra):
    lk = lake.get(name, {})
    img, usd = facade_image(name)
    if not img:
        img = lk.get("art_crop")  # fallback: art crop still renders something
    return {
        "name": lk.get("name", name), "requested_name": name,
        "qty": extra.get("qty", 1), "role": extra.get("role"),
        "cmc": lk.get("cmc") or extra.get("mana_value") or 0,
        "mana_cost": lk.get("mana_cost") or extra.get("mana_cost") or "",
        "type_line": lk.get("type_line") or extra.get("type_line") or "",
        "colors": lk.get("colors") or extra.get("colors") or [],
        "image_normal": img,
        "scryfall_uri": lk.get("scryfall_uri"),
        "usd": usd,
        "reason": extra.get("reason"), "cut": extra.get("cut"),
        "labels": extra.get("labels"),
    }

deck_e = [enrich(c["name"], dict(qty=c.get("quantity"), role=c.get("role"),
          mana_value=c.get("mana_value"), mana_cost=c.get("mana_cost"),
          type_line=c.get("type_line"), colors=c.get("colors"),
          labels=c.get("labels"))) for c in deck]
adds_e = [enrich(a["name"], dict(qty=a.get("qty", 1), reason=a.get("reason"),
          cut=a.get("cut"), labels=a.get("labels"))) for a in cs["adds"]]
con_e = [enrich(c["name"], dict(qty=1, reason=c.get("reason"),
          labels=c.get("labels"))) for c in cs["considerations"]]

def qmap(lst):
    return {e["name"]: e["qty"] for e in lst if e.get("qty") not in (None, 1)}

out = dict(deck=deck_e, adds=adds_e, considerations=con_e,
           changeset=dict(adds=[a["name"] for a in cs["adds"]],
                          drops=[d["name"] for d in cs["drops"]],
                          considerations=[c["name"] for c in cs["considerations"]],
                          addQty=qmap(adds_e), dropQty={}),
           label_order=cs.get("label_order", []))
json.dump(out, open(os.path.join(DATA, "enriched.json"), "w"), indent=2)

miss = [c["name"] for c in deck_e + adds_e + con_e if not c["image_normal"]]
real = sum(1 for c in deck_e + adds_e + con_e if c["image_normal"] and "art_crop" not in (c["image_normal"] or ""))
print(f"enriched {len(deck_e)} deck + {len(adds_e)} adds + {len(con_e)} consid; "
      f"{len(deck_e)+len(adds_e)+len(con_e)-len(miss)} have images ({len(miss)} still missing)")
if miss: print("missing:", miss)

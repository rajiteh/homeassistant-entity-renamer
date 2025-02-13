#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_from_directory
import re, json, ssl, websocket, config, stringcase
from contextlib import contextmanager

app = Flask(__name__)
TLS_S = 's' if config.TLS else ''

@contextmanager
def ha_websocket():
    """Context manager for a Home Assistant WebSocket connection with authentication."""
    ws_url = f'ws{TLS_S}://{config.HOST}/api/websocket'
    sslopt = {"cert_reqs": ssl.CERT_NONE} if not config.SSL_VERIFY else {}
    ws = websocket.WebSocket(sslopt=sslopt)
    ws.connect(ws_url)
    ws.recv()  # auth_required message
    ws.send(json.dumps({"type": "auth", "access_token": config.ACCESS_TOKEN}))
    auth_response = json.loads(ws.recv())
    if auth_response.get("type") != "auth_ok":
        ws.close()
        raise Exception("Authentication failed in HA WebSocket.")
    try:
        yield ws
    finally:
        ws.close()

def get_entity_friendly_name(ent):
    """
    Returns a human-friendly name for an entity.
    Uses the 'name', then 'original_name', or falls back to a sentence-cased version of the entity_id suffix.
    """
    ent_name = ent["entity_id"].split(".")[-1]
    return ent.get("name") or ent.get("original_name") or stringcase.sentencecase(ent_name)

def list_devices():
    """
    Retrieves the device registry from HA and returns a mapping of device_id to device name.
    """
    with ha_websocket() as ws:
        ws.send(json.dumps({"id": 2, "type": "config/device_registry/list"}))
        response = json.loads(ws.recv())
    if response.get("result") is None:
        return {}
    devices = {dev["id"]: dev.get("name_by_user") or dev["name"] for dev in response["result"]}
    return devices

def list_entities(search_regex=None):
    """
    Retrieves all entities (enabled/disabled) from HA's entity registry.
    Returns a list of tuples: (friendly_name, entity_id, device_id, device_name).
    Optionally filters the list by matching entity_id against search_regex.
    """
    with ha_websocket() as ws:
        ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
        response = json.loads(ws.recv())
    if response.get("result") is None:
        return []
    devices = list_devices()
    ents = response["result"]
    out = []
    for ent in ents:
        fn = get_entity_friendly_name(ent)
        eid = ent["entity_id"]
        did = ent.get("device_id")
        dname = devices.get(did, "N/A") if did else "N/A"
        out.append((fn, eid, did, dname))
    if search_regex:
        out = [(fn, eid, did, dname) for fn, eid, did, dname in out if re.search(search_regex, eid)]
    return sorted(out, key=lambda x: x[0])

def get_area_entities(area):
    """
    Retrieves a list of entity IDs related to the specified area.
    """
    with ha_websocket() as ws:
        payload = {"id": 999, "type": "search/related", "item_type": "area", "item_id": area}
        ws.send(json.dumps(payload))
        response = json.loads(ws.recv())
    if response.get("success") and response.get("result", {}).get("entity"):
        return response["result"]["entity"]
    return []

def get_floor_entities(floor):
    """
    Retrieves a list of entity IDs related to the specified floor.
    """
    with ha_websocket() as ws:
        payload = {"id": 1000, "type": "search/related", "item_type": "floor", "item_id": floor}
        ws.send(json.dumps(payload))
        response = json.loads(ws.recv())
    if response.get("success") and response.get("result", {}).get("entity"):
        return response["result"]["entity"]
    return []

def filter_exclusions(entities, exclusions):
    """
    Removes entities whose entity_id matches any of the exclusion regex patterns.
    """
    if not exclusions:
        return entities
    filtered = []
    for row in entities:
        # row: (fn, eid, did, dname)
        if any(re.search(ex, row[1]) for ex in exclusions if ex):
            continue
        filtered.append(row)
    return filtered

def compute_rename_data(entities, search, replace, fname_search, fname_replace, dev_search, dev_replace):
    """
    For each entity, compute new values via regex substitutions.
    Returns a list of tuples:
      (friendly_name, entity_id, device_id, device_name,
       new_entity_id, new_friendly_name, new_device_name)
    Only produces a new value if the substitution changes the original.
    """
    out = []
    for fn, eid, did, dname in entities:
        new_eid = ""
        new_fn = ""
        new_dname = ""
        if search and replace:
            replaced = re.sub(search, replace, eid)
            if replaced != eid:
                new_eid = replaced
        if fname_search and fname_replace:
            replaced_fn = re.sub(fname_search, fname_replace, fn)
            if replaced_fn != fn:
                new_fn = replaced_fn
        if dev_search and dev_replace:
            replaced_dname = re.sub(dev_search, dev_replace, dname)
            if replaced_dname != dname:
                new_dname = replaced_dname
        out.append((fn, eid, did, dname, new_eid, new_fn, new_dname))
    return out

def rename_entities(rename_data):
    """
    Updates entities via HA websocket.
    For each entity row, if either entity_id or friendly name is updated, send a registry update.
    Returns a list of results.
    """
    results = []
    with ha_websocket() as ws:
        for i, (fn, eid, did, dname, new_eid, new_fn, new_dname) in enumerate(rename_data, 1):
            if not new_eid and not new_fn:
                continue
            payload = {"id": i, "type": "config/entity_registry/update", "entity_id": eid}
            if new_eid:
                payload["new_entity_id"] = new_eid if new_eid != '""' else None
            if new_fn:
                payload["name"] = new_fn if new_fn != '""' else None
            ws.send(json.dumps(payload))
            res = json.loads(ws.recv())
            if res.get("success"):
                results.append({
                    "entity_id": eid,
                    "new_entity_id": new_eid if new_eid else eid,
                    "new_friendly_name": new_fn if new_fn else fn,
                    "status": "success"
                })
            else:
                err = res.get("error", {}).get("message", "Unknown error")
                results.append({
                    "entity_id": eid,
                    "new_entity_id": new_eid if new_eid else eid,
                    "new_friendly_name": new_fn if new_fn else fn,
                    "status": "failed",
                    "error": err
                })
    return results

def rename_devices(rename_data):
    """
    Deduplicates device updates from rename_data and updates each device via HA websocket.
    Only sends an update if a new device name is computed.
    Returns a list of device update results.
    """
    device_updates = {}
    for row in rename_data:
        # row: (fn, eid, did, dname, new_eid, new_fn, new_dname)
        did = row[2]
        dname = row[3]
        new_dname = row[6]
        if new_dname and did:
            if did not in device_updates:
                device_updates[did] = (dname, new_dname)
    results = []
    if not device_updates:
        return results
    with ha_websocket() as ws:
        for i, (did, (curr_name, new_name)) in enumerate(device_updates.items(), 1):
            payload = {
                "id": i,
                "type": "config/device_registry/update",
                "device_id": did,
                "name_by_user": new_name if new_name != '""' else None
            }
            ws.send(json.dumps(payload))
            res = json.loads(ws.recv())
            if res.get("success"):
                results.append({
                    "device_id": did,
                    "new_device_name": new_name,
                    "status": "success"
                })
            else:
                err = res.get("error", {}).get("message", "Unknown error")
                results.append({
                    "device_id": did,
                    "new_device_name": new_name,
                    "status": "failed",
                    "error": err
                })
    return results

def summarize_changes(rename_data):
    """
    Computes summary counts from rename_data.
    Returns a tuple:
      (entity_id_updates, friendly_name_updates, device_updates)
    """
    entity_updates = 0
    friendly_updates = 0
    device_updates = {}
    for fn, eid, did, dname, new_eid, new_fn, new_dname in rename_data:
        if new_eid:
            entity_updates += 1
        if new_fn:
            friendly_updates += 1
        if new_dname and did:
            device_updates[did] = new_dname
    return entity_updates, friendly_updates, len(device_updates)

@app.route('/')
def index():
    """Serve the main HTML page."""
    return send_from_directory('.', 'index.html')

@app.route('/search', methods=['GET'])
def search():
    """
    Preview endpoint:
    Returns a list of entities with computed rename values.
    Supports filtering by:
      - entity search/replace (regex)
      - friendly name search/replace (regex)
      - device name search/replace (regex)
      - exclusions (list of regex)
      - area filter (only return entities in the specified area)
      - floor filter (only return entities on the specified floor)
    """
    search_regex = request.args.get('search', '')
    replace = request.args.get('replace', '')
    fname_search = request.args.get('fname_search', '')
    fname_replace = request.args.get('fname_replace', '')
    dev_search = request.args.get('dev_search', '')
    dev_replace = request.args.get('dev_replace', '')
    exclusions = request.args.getlist('exclude')
    area = request.args.get('area', '').strip()
    floor = request.args.get('floor', '').strip()

    ents = list_entities(search_regex) if search_regex else list_entities()
    ents = filter_exclusions(ents, exclusions)
    if area:
        area_entities = set(get_area_entities(area))
        ents = [(fn, eid, did, dname) for fn, eid, did, dname in ents if eid in area_entities]
    if floor:
        floor_entities = set(get_floor_entities(floor))
        ents = [(fn, eid, did, dname) for fn, eid, did, dname in ents if eid in floor_entities]
    data = compute_rename_data(ents, search_regex, replace, fname_search, fname_replace, dev_search, dev_replace)
    return jsonify([
        {"friendly_name": fn, "entity_id": eid, "device_id": did, "device_name": dname,
         "new_entity_id": ne, "new_friendly_name": nfn, "new_device_name": ndn}
        for fn, eid, did, dname, ne, nfn, ndn in data
    ])

@app.route('/apply', methods=['POST'])
def apply():
    """
    Apply endpoint:
    Performs the entity and device renames using the provided parameters.
    Supports the same filters as /search, including area and floor filters.
    Deduplicates device updates.
    
    When called without confirm=true, returns a dry run summary:
      "Dry run: Updating X devices, Y entity IDs and Z friendly names."
    When confirm=true is provided as a query parameter, then the updates are executed.
    """
    j = request.json
    search_regex = j.get('search', '')
    replace = j.get('replace', '')
    fname_search = j.get('fname_search', '')
    fname_replace = j.get('fname_replace', '')
    dev_search = j.get('dev_search', '')
    dev_replace = j.get('dev_replace', '')
    exclusions = j.get('exclude', [])
    area = j.get('area', '').strip()
    floor = j.get('floor', '').strip()

    ents = list_entities(search_regex) if search_regex else list_entities()
    ents = filter_exclusions(ents, exclusions)
    if area:
        area_entities = set(get_area_entities(area))
        ents = [(fn, eid, did, dname) for fn, eid, did, dname in ents if eid in area_entities]
    if floor:
        floor_entities = set(get_floor_entities(floor))
        ents = [(fn, eid, did, dname) for fn, eid, did, dname in ents if eid in floor_entities]
    rename_data = compute_rename_data(ents, search_regex, replace, fname_search, fname_replace, dev_search, dev_replace)
    
    confirm = request.args.get('confirm', 'false').lower() == 'true'
    if not confirm:
        e_updates, f_updates, d_updates = summarize_changes(rename_data)
        summary = f"Dry run: Updating {d_updates} devices, {e_updates} entity IDs and {f_updates} friendly names."
        return jsonify({"dry_run": True, "message": summary})
    
    entity_results = rename_entities(rename_data)
    device_results = rename_devices(rename_data)
    return jsonify({"entities": entity_results, "devices": device_results})

if __name__ == "__main__":
    app.run(debug=True)

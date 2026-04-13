#!/usr/bin/env python3
"""
Gateway UNIFICADO - tudo em uma porta só (8180)
Versão produção - domínio fixo app.omniassessoria.shop
Cloudflared sobe junto como subprocesso
"""
import json, os, time, threading, requests, datetime as dt, subprocess, re
from flask import Flask, request, send_from_directory

APP = Flask(__name__)

# ---------- configs ----------
BASE_URL      = "https://corretoraviver.ras.uccpbx.com/openapi/v1.0"
PR_WEBHOOK    = "https://api.pipe.run/v1/webhooks/webphones/7c09099c-ae5c-41cb-84c2-8719fd8fc330"
OUTPUT_DIR = r"C:\Users\Usuario\Downloads\onvox-certo-main\gravacoes"
TOKEN_FILE = r"C:\Users\Usuario\Downloads\onvox-certo-main\token.json"
PUBLIC_HOST   = "https://corretoraviver.com"

# Token do tunnel nomeado no Cloudflare (domínio fixo)
CLOUDFLARE_TOKEN = "eyJhIjoiZGJhYmQ2NDA3OWRkNzRkODVmNDMyYTBiYmRhYWMwMWMiLCJ0IjoiOGI1OWY2NWMtY2Y1MC00ODIzLThhMTYtZWQ4ZDIyOWRlMjA0IiwicyI6IlpHWTVOVEJrWlRRdE5EUXhPQzAwWWpaakxUaGlaREl0WVROak1EVXpORE5oWW1JMiJ9"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------- Cloudflare tunnel ----------
def iniciar_cloudflared():
    print("🌐 Subindo cloudflared...")
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", CLOUDFLARE_TOKEN],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    for linha in proc.stdout:
        linha = linha.strip()
        if linha:
            print("[cloudflared] {linha}")
    print("⚠️ cloudflared encerrou.")

# ---------- token manager ----------
lock = threading.Lock()
token_data = {"access": "", "refresh": ""}

def load_tokens():
    global token_data
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            tk = json.load(f)
            token_data.update(tk)

def save_tokens():
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f)

def renovar_token():
    with lock:
        r = requests.post(f"{BASE_URL}/refresh_token",
                          json={"refresh_token": token_data["refresh"]},
                          headers={"User-Agent": "OpenAPI"})
        if r.status_code == 200 and r.json().get("errcode") == 0:
            body = r.json()
            token_data["access"]  = body["access_token"]
            token_data["refresh"] = body["refresh_token"]
            save_tokens()
            print("[TOKEN] Renovado:", token_data["access"][:20] + "...")
            return True
        else:
            print("[TOKEN] Erro na renovação:", r.text)
            return False

def token_thread():
    while True:
        time.sleep(25*60)
        renovar_token()

# ---------- requests com retry ----------
def parse_response(r):
    try:
        if not r.text or not r.text.strip():
            return {"errcode": -1, "errmsg": f"Resposta vazia (HTTP {r.status_code})"}
        return r.json()
    except Exception as e:
        return {"errcode": -1, "errmsg": f"JSON inválido: {r.text[:200]}"}

def api_get(path, params=None, raw=False):
    params = params or {}
    params["access_token"] = token_data["access"]
    r = requests.get(f"{BASE_URL}/{path}", params=params)
    if r.status_code == 401:
        if renovar_token():
            params["access_token"] = token_data["access"]
            r = requests.get(f"{BASE_URL}/{path}", params=params)
    return r if raw else parse_response(r)

def api_post(path, json_data=None, raw=False):
    url = f"{BASE_URL}/{path}?access_token={token_data['access']}"
    r = requests.post(url, json=json_data)
    if r.status_code == 401:
        if renovar_token():
            url = f"{BASE_URL}/{path}?access_token={token_data['access']}"
            r = requests.post(url, json=json_data)
    return r if raw else parse_response(r)

# ---------- lógica de chamadas ----------
call_map = {}

def segundos_para_hms(s):
    return str(dt.timedelta(seconds=s))

def baixar_gravacao(file_name):
    print(f"🎙️ Iniciando download: {file_name}")

    body = api_get("recording/download", {"file": file_name})
    if body.get("errcode") != 0:
        print(f"❌ Erro ao obter URL: {body}")
        return ""

    download_path = body.get("download_resource_url")
    if not download_path:
        print(f"❌ URL não retornada: {body}")
        return ""

    download_url = f"https://corretoraviver.ras.uccpbx.com{download_path}?access_token={token_data['access']}"
    print(f"📥 Baixando de: {download_url[:80]}...")

    resp = requests.get(download_url, stream=True)
    if resp.status_code != 200:
        print(f"❌ Erro ao baixar áudio (HTTP {resp.status_code})")
        return ""

    local_path = os.path.join(OUTPUT_DIR, file_name)
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(1024):
            if chunk:
                f.write(chunk)

    print(f"✅ Gravação salva: {local_path}")
    return f"{PUBLIC_HOST}/{file_name}"

def enviar_piperun(call_id, data):
    crm = call_map.pop(call_id, None)
    if not crm:
        print(f"⚠️ Call ID {call_id} não encontrado no mapa — ignorando.")
        return

    status_map = {"ANSWERED": 200, "NO ANSWER": 204, "BUSY": 486, "VOICEMAIL": 204}
    status = status_map.get(data["status"], 500)
    time_start = data["time_start"]
    dur_s = data["call_duration"]
    time_end = (dt.datetime.fromisoformat(time_start) +
                dt.timedelta(seconds=dur_s)).strftime("%Y-%m-%d %H:%M:%S")

    record_url = ""
    if data.get("recording"):
        try:
            record_url = baixar_gravacao(data["recording"])
        except Exception as e:
            print(f"❌ Erro ao baixar gravação: {e}")

    payload = {
        "id": crm["crm_id"],
        "start_at": time_start,
        "end_at": time_end,
        "status": status,
        "duration": segundos_para_hms(dur_s),
        "record_url": record_url or f"{PUBLIC_HOST}/sem-gravacao.mp3",
        "external_call_id": call_id,
        "cost": 0.0
    }
    print(f"📤 Enviando para Piperun: {payload}")
    resp = requests.post(PR_WEBHOOK, json=payload, headers={"Content-Type": "application/json"})
    print(f"✅ Piperun respondeu: {resp.status_code}")

# ---------- ROTAS ----------
@APP.route("/click", methods=["GET"])
@APP.route("/", methods=["GET"])
def click():
    user   = request.args.get("user")
    pwd    = request.args.get("pass")
    crm_id = request.args.get("id_crm_call")
    exten  = request.args.get("exten")
    dest   = request.args.get("destination")

    if not all([user, pwd, crm_id, exten, dest]):
        return f"""
        <h1>Gateway Telefonia OK ✅</h1>
        <p><b>URL pública:</b> {PUBLIC_HOST}</p>
        <ul>
            <li><b>/click</b> - Click-to-call (GET)</li>
            <li><b>/webhook</b> - Eventos do PABX (POST)</li>
            <li><b>/&lt;arquivo&gt;</b> - Download de gravações (GET)</li>
        </ul>
        <p><b>Configure no PABX:</b> {PUBLIC_HOST}/webhook</p>
        """, 200

    print(f"📞 Click-to-call: {exten} → {dest} (CRM ID: {crm_id})")

    if user != "piperun" or pwd != "1qaz2wsx":
        print("❌ Autenticação falhou")
        return "Unauthorized", 401

    body = api_post("call/dial", {"caller": exten, "callee": dest, "auto_answer": "yes"})
    if body.get("errcode") == 0:
        call_id = body["call_id"]
        call_map[call_id] = {"crm_id": int(crm_id)}
        print(f"✅ Ligação iniciada. Call ID: {call_id}")
    else:
        print(f"❌ Erro ao discar: {body}")
        return f"Erro ao discar: {body.get('errmsg', 'desconhecido')}", 500

    return "", 200

@APP.route("/webhook", methods=["POST"])
@APP.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    msg_data = None
    msg_key = "msg" if "msg" in data else "message" if "message" in data else None
    if msg_key and isinstance(data[msg_key], str):
        try:
            msg_data = json.loads(data[msg_key])
            print(f"📋 Dados do {msg_key} parseados: {json.dumps(msg_data, indent=2)}")
        except json.JSONDecodeError:
            print(f"⚠️ Erro ao parsear {msg_key}: {data[msg_key]}")

    webhook_data = msg_data if msg_data else data
    call_id = webhook_data.get("call_id")
    event   = data.get("event", "")

    print(f"📥 Webhook recebido - Event: {event}, Call ID: {call_id}")
    print(f"📋 Dados completos: {json.dumps(data, indent=2)}")

    if call_id and "time_start" in webhook_data:
        print(f"⏱️ Agendando envio para Piperun em 1s...")
        threading.Timer(1.0, enviar_piperun, args=[call_id, webhook_data]).start()
    else:
        print(f"⚠️ Webhook ignorado (sem call_id ou time_start)")
        print(f"🔍 webhook_data disponível: {list(webhook_data.keys())}")

    return "", 200

@APP.route("/<path:filename>")
def download(filename):
    if filename in ["webhook", "click"]:
        return "Not Found", 404
    print(f"📥 Download solicitado: {filename}")
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)

# ---------- START ----------
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    print("=" * 60)
    print("🚀 Iniciando Gateway Unificado de Telefonia")
    print("=" * 60)

    load_tokens()

    print("🔑 Renovando token inicial...")
    renovar_token()

    print("⏰ Iniciando thread de renovação automática...")
    threading.Thread(target=token_thread, daemon=True).start()

    print("🌐 Subindo cloudflared em background...")
    threading.Thread(target=iniciar_cloudflared, daemon=True).start()

    print("=" * 60)
    print(f"✅ Gateway rodando em http://0.0.0.0:8180")
    print(f"🌐 URL pública: {PUBLIC_HOST}")
    print("=" * 60)
    print(f"\n   PABX Webhook URL: {PUBLIC_HOST}/webhook")
    print(f"   Click-to-call:    {PUBLIC_HOST}/click?user=piperun&pass=1qaz2wsx&...")
    print(f"   Gravações:        {PUBLIC_HOST}/<arquivo>.wav\n")
    print("=" * 60 + "\n")

    APP.run(host="0.0.0.0", port=8180, debug=False)
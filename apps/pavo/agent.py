# FILE: agent.py
import argparse, datetime
from flask import Flask, request, jsonify, make_response
import requests

app = Flask(__name__)
ALLOWED_ORIGINS = set()


def _cors_headers(origin):
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Accept",
        "Access-Control-Max-Age": "600",
        "Access-Control-Allow-Private-Network": "true",
    }


def _maybe_cors(resp, origin):
    if origin and ((origin in ALLOWED_ORIGINS) or ("*" in ALLOWED_ORIGINS)):
        for k, v in _cors_headers(origin).items():
            resp.headers[k] = v
    return resp


@app.after_request
def add_security_headers(resp):
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        return _maybe_cors(make_response("", 204), origin)
    body = {"ok": True, "agent": "pavo", "time": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    return _maybe_cors(make_response(jsonify(body), 200), origin)


@app.route("/pairing", methods=["POST", "OPTIONS"])
def pairing():
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        return _maybe_cors(make_response("", 204), origin)
    try:
        data = request.get_json(force=True)
        ip = data["ip"].strip()
        secure = bool(data.get("secure"))
        port = int(data.get("port") or (4567 if secure else 4568))
        sn = data["serial_number"].strip()
        fp = data["fingerprint"].strip()
        scheme = "https" if secure else "http"
        url = f"{scheme}://{ip}:{port}/Pairing"
        payload = {"TransactionHandle": {"SerialNumber": sn, "Fingerprint": fp, "TransactionSequence": 1,
                                         "TransactionDate": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}}
        r = requests.post(url, json=payload, timeout=8, verify=False)
        r.raise_for_status()
        body = {"ok": True, "endpoint": url, "response": r.json()}
        code = 200
    except Exception as e:
        body = {"ok": False, "error": str(e)}
        code = 502
    return _maybe_cors(make_response(jsonify(body), code), origin)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9099)
    p.add_argument("--cert", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--allow-origin", action="append", default=[])
    args = p.parse_args()
    ALLOWED_ORIGINS = set(args.allow_origin or [])
    app.run(host="127.0.0.1", port=args.port, ssl_context=(args.cert, args.key), threaded=True)

import os
import sys
import psycopg2
import requests
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import SimpleConnectionPool
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from functools import wraps
import json
import logging


class JsonFormatter(logging.Formatter):
    """ Log em JSON (uma linha por evento) para o Alloy/Loki. Os campos otelTraceID e
    otelSpanID são injetados pela instrumentação de logging do OpenTelemetry
    (OTEL_PYTHON_LOG_CORRELATION=true) e permitem pular do log para o trace. """

    def format(self, record):
        entry = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = getattr(record, "otelTraceID", "0")
        if trace_id and trace_id != "0":
            entry["trace_id"] = trace_id
            entry["span_id"] = getattr(record, "otelSpanID", "")
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


# Configura o logging (force=True substitui o handler que o opentelemetry-instrument
# já tenha criado, mantendo a injeção de trace_id/span_id nos registros)
_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger(__name__)

# Carrega .env para desenvolvimento local
load_dotenv()

app = Flask(__name__)

# --- Configuração ---
DATABASE_URL = os.getenv("DATABASE_URL")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL")

if not DATABASE_URL or not AUTH_SERVICE_URL:
    log.critical("Erro: DATABASE_URL e AUTH_SERVICE_URL devem ser definidos.")
    sys.exit(1)

# --- Pool de Conexão com o Banco ---
try:
    # RealDictCursor é definido na conexão (e não em cada conn.cursor()) para que a
    # instrumentação do psycopg2 continue gerando os spans das queries
    pool = SimpleConnectionPool(1, 5, dsn=DATABASE_URL, cursor_factory=RealDictCursor)
    log.info("Pool de conexões com o PostgreSQL (targeting) inicializado.")
except psycopg2.OperationalError as e:
    log.critical(f"Erro fatal ao conectar ao PostgreSQL: {e}")
    sys.exit(1)

# --- Middleware de Autenticação (Idêntico ao flag-service) ---


def require_auth(f):
    """ Middleware para validar a chave de API contra o auth-service """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return jsonify({"error": "Authorization header obrigatório"}), 401

        try:
            validate_url = f"{AUTH_SERVICE_URL}/validate"
            response = requests.get(validate_url, headers={"Authorization": auth_header}, timeout=3)

            if response.status_code != 200:
                log.warning(f"Falha na validação da chave (status: {response.status_code})")
                return jsonify({"error": "Chave de API inválida"}), 401

        except requests.exceptions.Timeout:
            log.error("Timeout ao conectar com o auth-service")
            return jsonify({"error": "Serviço de autenticação indisponível (timeout)"}), 504  # Gateway Timeout
        except requests.exceptions.RequestException as e:
            log.error(f"Erro ao conectar com o auth-service: {e}")
            return jsonify({"error": "Serviço de autenticação indisponível"}), 503  # Service Unavailable

        return f(*args, **kwargs)
    return decorated

# --- Endpoints da API ---


@app.route('/health')
def health():
    return jsonify({"status": "ok"})


@app.route('/rules', methods=['POST'])
@require_auth
def create_rule():
    """ Cria uma nova regra de segmentação para uma flag """
    data = request.get_json()
    if not data or 'flag_name' not in data or 'rules' not in data:
        return jsonify({"error": "'flag_name' e 'rules' (JSON) são obrigatórios"}), 400

    flag_name = data['flag_name']
    rules_obj = data['rules']  # O objeto JSON
    is_enabled = data.get('is_enabled', True)

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO targeting_rules (flag_name, is_enabled, rules, created_at, updated_at) "
            "VALUES (%s, %s, %s, NOW(), NOW()) RETURNING *",
            (flag_name, is_enabled, Json(rules_obj))  # Usa Json() para serializar
        )
        new_rule = cur.fetchone()
        conn.commit()
        log.info(f"Regra para '{flag_name}' criada com sucesso.")
        return jsonify(new_rule), 201
    except psycopg2.IntegrityError:
        if conn:
            conn.rollback()
        log.warning(f"Tentativa de criar regra duplicada: '{flag_name}'")
        return jsonify({"error": f"Regra para a flag '{flag_name}' já existe"}), 409
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Erro ao criar regra: {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


@app.route('/rules/<string:flag_name>', methods=['GET'])
@require_auth
def get_rule(flag_name):
    """ Busca uma regra de segmentação pelo nome da flag """
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM targeting_rules WHERE flag_name = %s", (flag_name,))
        rule = cur.fetchone()
        if not rule:
            return jsonify({"error": "Regra não encontrada"}), 404
        return jsonify(rule)
    except Exception as e:
        log.error(f"Erro ao buscar regra '{flag_name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


@app.route('/rules/<string:flag_name>', methods=['PUT'])
@require_auth
def update_rule(flag_name):
    """ Atualiza a regra de segmentação de uma flag """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Corpo da requisição obrigatório"}), 400

    fields = []
    values = []

    if 'rules' in data:
        fields.append("rules = %s")
        values.append(Json(data['rules']))  # Serializa o JSON
    if 'is_enabled' in data:
        fields.append("is_enabled = %s")
        values.append(data['is_enabled'])

    if not fields:
        return jsonify({"error": "Pelo menos um campo ('rules', 'is_enabled') é obrigatório"}), 400

    values.append(flag_name)  # Adiciona o 'flag_name' para a cláusula WHERE

    query = f"UPDATE targeting_rules SET {', '.join(fields)} WHERE flag_name = %s RETURNING *"

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute(query, tuple(values))

        if cur.rowcount == 0:
            return jsonify({"error": "Regra não encontrada"}), 404

        updated_rule = cur.fetchone()
        conn.commit()
        log.info(f"Regra para '{flag_name}' atualizada com sucesso.")
        return jsonify(updated_rule), 200
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Erro ao atualizar regra '{flag_name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


@app.route('/rules/<string:flag_name>', methods=['DELETE'])
@require_auth
def delete_rule(flag_name):
    """ Deleta a regra de segmentação de uma flag """
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("DELETE FROM targeting_rules WHERE flag_name = %s", (flag_name,))

        if cur.rowcount == 0:
            return jsonify({"error": "Regra não encontrada"}), 404

        conn.commit()
        log.info(f"Regra para '{flag_name}' deletada com sucesso.")
        return "", 204  # 204 No Content
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Erro ao deletar regra '{flag_name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


if __name__ == '__main__':
    port = int(os.getenv("PORT", 8003))
    app.run(host='0.0.0.0', port=port, debug=False)

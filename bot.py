import os
import time
import logging

from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt

# =========================
# 환경 변수 로드 (.env)
# =========================
load_dotenv()

OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")
OKX_TESTNET = os.getenv("OKX_TESTNET", "false").lower() == "true"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# =========================
# 기본 설정
# =========================

# 이더리움 USDT 선물 심볼
# - ccxt 버전에 따라 "ETH/USDT:USDT" 또는 "ETH-USDT-SWAP" 일 수 있음
# - 나중에 에러 나면 exchange.load_markets() 로 심볼 리스트 확인해서 수정
SYMBOL = "ETH/USDT:USDT"

# 한 번 주문할 때 사용할 USDT 기준 포지션 크기
POSITION_SIZE_USDT = 50     # 예: 50 USDT

# 레버리지 (OKX 웹/앱 설정과 맞춰두면 좋음)
LEVERAGE = 10

# 마진 모드: "cross" 또는 "isolated"
MARGIN_MODE = "cross"

# 로그 폴더/파일 설정
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "trading.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                 # 콘솔 출력
        logging.FileHandler(LOG_FILE, encoding="utf-8")  # 파일 저장
    ]
)

# =========================
# CCXT OKX 객체 생성
# =========================

okx_config = {
    "apiKey": OKX_API_KEY,
    "secret": OKX_API_SECRET,
    "password": OKX_API_PASSWORD,   # OKX는 passphrase를 password로 사용
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",      # 선물/퍼페추얼
    },
}

exchange = ccxt.okx(okx_config)

# 테스트넷 사용 시
if OKX_TESTNET:
    # ccxt에서 sandbox 모드를 지원
    exchange.set_sandbox_mode(True)
    logging.info("OKX TESTNET(SANDBOX) 모드로 실행 중")
else:
    logging.info("OKX REAL(실거래) 모드로 실행 중")


# =========================
# 유틸 함수들
# =========================

def fetch_price(symbol: str) -> float:
    """현재 가격 조회"""
    ticker = exchange.fetch_ticker(symbol)
    price = ticker["last"]
    logging.info(f"{symbol} 현재가: {price}")
    return price


def calc_contract_size(symbol: str, usdt_size: float) -> float:
    """
    USDT 금액 기준으로 몇 코인(계약)을 살지 계산.
    amount = usdt_size / price
    """
    price = fetch_price(symbol)
    amount = usdt_size / price
    # 너무 작은 소수 방지용 간단 반올림 (ETH 최소 수량에 맞게 조정 가능)
    amount = float(f"{amount:.4f}")
    logging.info(f"주문 수량 계산: {usdt_size} USDT -> {amount} {symbol}")
    return amount


def get_position(symbol: str):
    """
    현재 포지션 정보 조회 (One-way / Net 모드 기준).
    return: (side, size)
      - side: "LONG" / "SHORT" / "FLAT"
      - size: 코인 수
    """
    try:
        positions = exchange.fetch_positions([symbol])
    except Exception as e:
        logging.error(f"포지션 조회 실패: {e}")
        return "FLAT", 0.0

    side = "FLAT"
    size = 0.0

    for p in positions:
        if p["symbol"] != symbol:
            continue

        contracts = float(p.get("contracts") or 0)
        if contracts > 0:
            side = p["side"].upper()  # 'long' 또는 'short'
            size = contracts
            break

    logging.info(f"현재 포지션: side={side}, size={size}")
    return side, size


def close_position(symbol: str, side: str, size: float):
    """
    현재 포지션 전체 청산.
    side: "LONG" 또는 "SHORT"
    size: 코인 수
    """
    if size <= 0:
        logging.info("청산할 포지션이 없음.")
        return

    params = {
        "tdMode": MARGIN_MODE,
        "reduceOnly": True,  # 포지션 줄이기/청산만 허용
    }

    try:
        if side == "LONG":
            logging.info(f"[청산] LONG {size} {symbol}")
            order = exchange.create_order(symbol, "market", "sell", size, None, params)
        elif side == "SHORT":
            logging.info(f"[청산] SHORT {size} {symbol}")
            order = exchange.create_order(symbol, "market", "buy", size, None, params)
        else:
            logging.warning(f"알 수 없는 side={side}, 청산 불가.")
            return

        logging.info(f"청산 주문 결과: {order}")
    except Exception as e:
        logging.error(f"포지션 청산 실패: {e}")


def open_long(symbol: str, size: float):
    """롱 포지션 진입"""
    params = {
        "tdMode": MARGIN_MODE,
        "lever": str(LEVERAGE),
        "reduceOnly": False,
    }
    try:
        logging.info(f"[진입] LONG {size} {symbol}")
        order = exchange.create_order(symbol, "market", "buy", size, None, params)
        logging.info(f"롱 진입 결과: {order}")
    except Exception as e:
        logging.error(f"롱 진입 실패: {e}")


def open_short(symbol: str, size: float):
    """숏 포지션 진입"""
    params = {
        "tdMode": MARGIN_MODE,
        "lever": str(LEVERAGE),
        "reduceOnly": False,
    }
    try:
        logging.info(f"[진입] SHORT {size} {symbol}")
        order = exchange.create_order(symbol, "market", "sell", size, None, params)
        logging.info(f"숏 진입 결과: {order}")
    except Exception as e:
        logging.error(f"숏 진입 실패: {e}")


def handle_signal(signal: str):
    """
    TradingView 에서 온 신호("BUY" 또는 "SELL") 처리.
    - BUY: 숏이면 청산 후 롱 진입, 이미 롱이면 아무것도 안 함
    - SELL: 롱이면 청산 후 숏 진입, 이미 숏이면 아무것도 안 함
    """
    signal = signal.upper().strip()
    logging.info(f"수신한 신호: {signal}")

    current_side, current_size = get_position(SYMBOL)
    new_size = calc_contract_size(SYMBOL, POSITION_SIZE_USDT)

    if signal == "BUY":
        if current_side == "LONG":
            logging.info("이미 LONG 포지션, 추가 행동 없음.")
            return

        if current_side == "SHORT":
            close_position(SYMBOL, current_side, current_size)
            time.sleep(0.5)

        open_long(SYMBOL, new_size)

    elif signal == "SELL":
        if current_side == "SHORT":
            logging.info("이미 SHORT 포지션, 추가 행동 없음.")
            return

        if current_side == "LONG":
            close_position(SYMBOL, current_side, current_size)
            time.sleep(0.5)

        open_short(SYMBOL, new_size)

    else:
        logging.warning(f"알 수 없는 신호: {signal}")


# =========================
# Flask 웹서버 (TradingView Webhook)
# =========================

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    TradingView Webhook 이 호출하는 엔드포인트.
    예시 메시지:

    {
      "secret": "내가_정한_비밀번호",
      "signal": "BUY"
    }
    """
    data = request.get_json(force=True)

    # 보안용 secret 체크
    if WEBHOOK_SECRET:
        if data.get("secret") != WEBHOOK_SECRET:
            logging.warning("잘못된 secret 으로 접근 시도.")
            return jsonify({"status": "error", "message": "invalid secret"}), 403

    signal = data.get("signal")
    if signal is None:
        return jsonify({"status": "error", "message": "no signal field"}), 400

    handle_signal(signal)

    return jsonify({"status": "ok", "received_signal": signal})


if __name__ == "__main__":
    # 로컬에서 실행: python bot.py
    # 서버에서 실행할 땐 host="0.0.0.0" 으로 두면 외부에서 접근 가능
    app.run(host="0.0.0.0", port=5000)


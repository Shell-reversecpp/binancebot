from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import *
from colorama import Fore, init
from decimal import Decimal
from pyngrok import ngrok
from queue import Queue
from threading import Lock
import time

# Очередь команд и блокировка для синхронизации
command_queue = Queue()
lock = Lock()


# Инициализация Colorama для цветного вывода
init(autoreset=True)

# Ваши API-ключи
api_key = "Ny4y3EMrdzq0Xes109B3SK027usxx6WO2HIgeMiKNdKKuiC6TLTDvU2lJv54qoQv"
api_secret = "ma90EU7TZJnVyN58UExyxnqkS2VUx7QzY9LgefU0Wu61PbmZKlUIHyk7bhzhDjLy"

# Создание клиента Binance
print(Fore.YELLOW + "Инициализация Binance API...")
client = Client(api_key, api_secret)

# Flask приложение
app = Flask(__name__)

def get_lot_filter(symbol):
    info = client.get_symbol_info(symbol)
    for filter_info in info['filters']:
        if filter_info['filterType'] == 'LOT_SIZE':
            step_size = Decimal(filter_info['stepSize'])
            min_qty = Decimal(filter_info['minQty'])
            max_qty = Decimal(filter_info['maxQty'])
            return step_size, min_qty, max_qty
    raise ValueError(f"LOT_SIZE фильтр не найден для {symbol}")


def get_symbol_filters(symbol):
    """Получает фильтры для символа (например, LOT_SIZE и PRICE_FILTER)."""
    try:
        symbol_info = client.get_symbol_info(symbol)
        filters = {f['filterType']: f for f in symbol_info['filters']}
        return filters
    except Exception as e:
        print(Fore.RED + f"❌ Ошибка получения фильтров для {symbol}: {str(e)}")
        return {}

def adjust_quantity(quantity, step_size):
    return (Decimal(quantity) // Decimal(step_size)) * Decimal(step_size)

def repay_remaining_debt(symbol, asset_key):
    """Погашение долга для указанного актива в позиции."""
    try:
        while True:
            margin_account = client.get_isolated_margin_account()
            asset_info = next((item for item in margin_account['assets'] if item['symbol'] == symbol), None)

            if not asset_info:
                print(Fore.RED + f"Активы для {symbol} не найдены.")
                break

            borrowed = Decimal(asset_info[asset_key]['borrowed'])
            free_balance = Decimal(asset_info[asset_key]['free'])

            # Если долг меньше минимального порога — завершаем
            if borrowed < Decimal('0.001'):
                print(Fore.GREEN + f"✅ Долг для {symbol} погашен до минимального значения.")
                break
 # Погашаем долг максимально доступными средствами
            repay_amount = min(borrowed, free_balance)
            client.repay_margin_loan(
                asset=asset_info[asset_key]['asset'],
                amount=str(repay_amount),
                symbol=symbol,
                isIsolated='TRUE'
            )
            print(Fore.GREEN + f"Погашено {repay_amount} для {symbol}. Остаток долга: {borrowed - repay_amount}")
    except Exception as e:
        print(Fore.RED + f"Ошибка погашения долга: {str(e)}")

def get_current_price(symbol):
    """Получение текущей рыночной цены для указанного символа."""
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        return Decimal(ticker['price'])
    except Exception as e:
        print(Fore.RED + f"Ошибка получения текущей цены для {symbol}: {str(e)}")
        return Decimal('0')


def open_position(symbol, margin, leverage, position_type, max_retries=30):
    attempt = 0  # Счётчик попыток
    while attempt < max_retries:
        try:
            # Проверка на висящие лимитные ордера
            open_orders = client.get_open_margin_orders(symbol=symbol, isIsolated='TRUE')
            if open_orders:
                print(Fore.YELLOW + f"Найдено {len(open_orders)} висящих лимитных ордеров для {symbol}. Отменяем...")
                for order in open_orders:
                    result = client.cancel_margin_order(symbol=symbol, orderId=order['orderId'], isIsolated='TRUE')
                    print(Fore.CYAN + f"Отменён лимитный ордер ID: {order['orderId']} для {symbol}. Результат: {result}")
                print(Fore.GREEN + "Все лимитные ордера успешно отменены.")
            else:
                print(Fore.BLUE + f"Нет висящих лимитных ордеров для {symbol}. Продолжаем.")

            # Проверка и погашение долгов
            margin_account = client.get_isolated_margin_account()
            asset_info = next((item for item in margin_account['assets'] if item['symbol'] == symbol), None)
            if not asset_info:
                return {"status": "error", "message": f"Активы для {symbol} не найдены."}

            base_borrowed = Decimal(asset_info['baseAsset']['borrowed'])
            quote_borrowed = Decimal(asset_info['quoteAsset']['borrowed'])
            if base_borrowed > Decimal('0.001'):
                print(Fore.YELLOW + f"Обнаружен долг в базовом активе: {base_borrowed}. Погашаем...")
                repay_remaining_debt(symbol, 'baseAsset')
            if quote_borrowed > Decimal('0.001'):
                print(Fore.YELLOW + f"Обнаружен долг в котируемом активе: {quote_borrowed}. Погашаем...")
                repay_remaining_debt(symbol, 'quoteAsset')

            # Расчёт параметров позиции
            total_margin = Decimal(margin) * Decimal(leverage)
            current_price = get_current_price(symbol)
            step_size, min_qty, max_qty = get_lot_filter(symbol)
            raw_quantity = total_margin / current_price
            quantity = adjust_quantity(raw_quantity, step_size)

            if quantity < min_qty or quantity > max_qty:
                return {"status": "error", "message": f"Количество {quantity} выходит за пределы {min_qty}-{max_qty}"}
# Определение направления сделки
            if position_type == 'long':
                side = SIDE_BUY
                side_effect_type = 'MARGIN_BUY'
            elif position_type == 'short':
                side = SIDE_SELL
                side_effect_type = 'MARGIN_BUY'
            else:
                return {"status": "error", "message": "Некорректный тип позиции."}

            # Корректировка цены лимитного ордера
            price_adjustment = Decimal('0.0009')
            limit_price = current_price - price_adjustment if side == SIDE_BUY else current_price + price_adjustment
            limit_price = limit_price.quantize(Decimal('0.00000001'))  # Учитываем точность

            # Открытие лимитного ордера
            print(Fore.GREEN + f"Открываем {position_type} позицию на {quantity} {symbol} лимитным ордером по цене {limit_price}...")
            order = client.create_margin_order(
                symbol=symbol,
                side=side,
                type=ORDER_TYPE_LIMIT,
                price=str(limit_price),
                quantity=str(quantity),
                timeInForce=TIME_IN_FORCE_GTC,
                isIsolated='TRUE',
                sideEffectType=side_effect_type
            )

            order_id = order['orderId']
            time.sleep(25)  # Задержка в 10 секунд

            # Проверка, выполнен ли ордер
            order_status = client.get_margin_order(symbol=symbol, orderId=order_id, isIsolated='TRUE')
            if order_status['status'] == 'FILLED':
                return {"status": "success", "message": f"{position_type.capitalize()} позиция открыта", "order": order}
            else:
                print(Fore.YELLOW + f"Лимитный ордер не был выполнен. Повторяем попытку {attempt + 1}...")
                client.cancel_margin_order(symbol=symbol, orderId=order_id, isIsolated='TRUE')
                attempt += 1
                continue

        except Exception as e:
            print(Fore.RED + f"Ошибка при открытии позиции: {str(e)}")
            error_message = str(e)
            if "Exceeding the account's maximum borrowable limit" in error_message or \
               "Mandatory parameter 'amount' was not sent" in error_message:
                attempt += 1
                continue
            else:
                return {"status": "error", "message": error_message}

    return {"status": "error", "message": "Не удалось открыть позицию после нескольких попыток."}

def close_position(symbol, margin, leverage, position_type):
    try:
        # Получаем информацию о маржинальном аккаунте
        margin_account = client.get_isolated_margin_account()
        position = next((asset for asset in margin_account['assets'] if asset['symbol'] == symbol), None)

        if not position:
            return {"status": "error", "message": f"Нет открытых позиций для {symbol}"}

        borrowed_base = Decimal(position['baseAsset']['borrowed'])
        borrowed_quote = Decimal(position['quoteAsset']['borrowed'])
 # Проверяем тип позиции и вызываем соответствующую функцию
        if position_type == 'long':
            return close_long_position(symbol)
        elif position_type == 'short':
            return close_short_position(symbol)
        else:
            return {"status": "error", "message": f"Неизвестный тип позиции: {position_type}"}

    except Exception as e:
        print(Fore.RED + f"Ошибка при закрытии позиции: {str(e)}")
        return {"status": "error", "message": str(e)}

def close_long_position(symbol, max_retries=20):
    """Закрытие LONG позиции с использованием лимитных ордеров."""
    attempt = 0  # Счётчик попыток
    while attempt < max_retries:
        try:
            # Проверка на висящие лимитные ордера на маржинальном изолированном счёте
            open_orders = client.get_open_margin_orders(symbol=symbol, isIsolated='TRUE')
            if open_orders:
                print(Fore.YELLOW + f"Найдено {len(open_orders)} висящих лимитных ордеров для {symbol}. Отменяем...")
                for order in open_orders:
                    result = client.cancel_margin_order(symbol=symbol, orderId=order['orderId'], isIsolated='TRUE')
                    print(Fore.CYAN + f"Отменён лимитный ордер ID: {order['orderId']} для {symbol}. Результат: {result}")
                print(Fore.GREEN + "Все лимитные ордера успешно отменены.")
            else:
                print(Fore.BLUE + f"Нет висящих лимитных ордеров для {symbol}. Продолжаем.")

            # Получение информации о позиции
            margin_account = client.get_isolated_margin_account()
            position = next((asset for asset in margin_account['assets'] if asset['symbol'] == symbol), None)

            if not position:
                return {"status": "error", "message": f"Нет открытой LONG позиции для {symbol}"}

            base_free = Decimal(position['baseAsset']['free'])

            if base_free <= 0:
                return {"status": "error", "message": "Недостаточно базового актива для закрытия LONG позиции"}

            # Получаем параметры лота
            step_size, min_qty, max_qty = get_lot_filter(symbol)
            quantity = adjust_quantity(base_free, step_size)

            if quantity < min_qty or quantity > max_qty:
                return {"status": "error", "message": f"Количество {quantity} выходит за пределы {min_qty}-{max_qty}"}

            # Получение текущей цены и корректировка лимитной цены
            current_price = get_current_price(symbol)
            price_adjustment = Decimal('0.0009')  # Смещение цены для лимитного ордера
            limit_price = current_price + price_adjustment
            limit_price = limit_price.quantize(Decimal('0.00000001'))  # Учитываем точность

            # Создание лимитного ордера на продажу
            print(Fore.GREEN + f"Закрываем LONG позицию на {quantity} {symbol} лимитным ордером по цене {limit_price}...")
            order = client.create_margin_order(
                symbol=symbol,
                side=SIDE_SELL,
                type=ORDER_TYPE_LIMIT,
                price=str(limit_price),
                quantity=str(quantity),
                timeInForce=TIME_IN_FORCE_GTC,
                isIsolated='TRUE',
                sideEffectType='AUTO_REPAY'
            )
            order_id = order['orderId']
            time.sleep(10)  # Задержка в 10 секунд

            # Проверка статуса ордера
            order_status = client.get_margin_order(symbol=symbol, orderId=order_id, isIsolated='TRUE')
            if order_status['status'] == 'FILLED':
                print(Fore.GREEN + f"✅ LONG позиция успешно закрыта: {order}")
                return {"status": "success", "message": "LONG позиция закрыта", "order": order}
            else:
                print(Fore.YELLOW + f"Лимитный ордер не был выполнен. Повторяем попытку {attempt + 1}...")
                client.cancel_margin_order(symbol=symbol, orderId=order_id, isIsolated='TRUE')
                attempt += 1
                continue

        except Exception as e:
            print(Fore.RED + f"Ошибка при закрытии LONG позиции: {str(e)}")
            attempt += 1
            continue

    return {"status": "error", "message": "Не удалось закрыть LONG позицию после нескольких попыток."}

def close_short_position(symbol, max_retries=20):
    """Закрытие SHORT позиции с использованием лимитных ордеров."""
    attempt = 0  # Счётчик попыток
    while attempt < max_retries:
        try:
            # Проверка на висящие лимитные ордера на маржинальном изолированном счёте
            open_orders = client.get_open_margin_orders(symbol=symbol, isIsolated='TRUE')
            if open_orders:
                print(Fore.YELLOW + f"Найдено {len(open_orders)} висящих лимитных ордеров для {symbol}. Отменяем...")
                for order in open_orders:
                    result = client.cancel_margin_order(symbol=symbol, orderId=order['orderId'], isIsolated='TRUE')
                    print(Fore.CYAN + f"Отменён лимитный ордер ID: {order['orderId']} для {symbol}. Результат: {result}")
                print(Fore.GREEN + "Все лимитные ордера успешно отменены.")
            else:
                print(Fore.BLUE + f"Нет висящих лимитных ордеров для {symbol}. Продолжаем.")

            # Получение информации о позиции
            margin_account = client.get_isolated_margin_account()
            position = next((asset for asset in margin_account['assets'] if asset['symbol'] == symbol), None)

            if not position:
                return {"status": "error", "message": f"Нет открытой SHORT позиции для {symbol}"}

            borrowed_base = Decimal(position['baseAsset']['borrowed'])
            if borrowed_base <= 0:
                return {"status": "error", "message": "Нет заимствованного базового актива для закрытия SHORT позиции"}

            # Получаем параметры лота
            step_size, min_qty, max_qty = get_lot_filter(symbol)
            quantity = adjust_quantity(borrowed_base, step_size)

            if quantity < min_qty or quantity > max_qty:
                return {"status": "error", "message": f"Количество {quantity} выходит за пределы {min_qty}-{max_qty}"}

            # Получение текущей цены и корректировка лимитной цены
            current_price = get_current_price(symbol)
            price_adjustment = Decimal('0.0009')  # Смещение цены для лимитного ордера
            limit_price = current_price - price_adjustment
            limit_price = limit_price.quantize(Decimal('0.00000001'))  # Учитываем точность

            # Создание лимитного ордера на покупку
            print(Fore.GREEN + f"Закрываем SHORT позицию на {quantity} {symbol} лимитным ордером по цене {limit_price}...")
            order = client.create_margin_order(
                symbol=symbol,
                side=SIDE_BUY,
                type=ORDER_TYPE_LIMIT,
                price=str(limit_price),
                quantity=str(quantity),
                timeInForce=TIME_IN_FORCE_GTC,
                isIsolated='TRUE',
                sideEffectType='AUTO_REPAY'
            )
            order_id = order['orderId']
            time.sleep(10)  # Задержка в 10 секунд

            # Проверка статуса ордера
            order_status = client.get_margin_order(symbol=symbol, orderId=order_id, isIsolated='TRUE')
            if order_status['status'] == 'FILLED':
                print(Fore.GREEN + f"✅ SHORT позиция успешно закрыта: {order}")
                return {"status": "success", "message": "SHORT позиция закрыта", "order": order}
            else:
                print(Fore.YELLOW + f"Лимитный ордер не был выполнен. Повторяем попытку {attempt + 1}...")
                client.cancel_margin_order(symbol=symbol, orderId=order_id, isIsolated='TRUE')
                attempt += 1
                continue

        except Exception as e:
            print(Fore.RED + f"Ошибка при закрытии SHORT позиции: {str(e)}")
            attempt += 1
            continue

    return {"status": "error", "message": "Не удалось закрыть SHORT позицию после нескольких попыток."}


def check_open_position(symbol, position_type):
    """Проверяет, есть ли открытая позиция указанного типа (long/short)."""
    try:
        # Получаем изолированный маржинальный аккаунт
        margin_account = client.get_isolated_margin_account()
        
        # Находим информацию по символу
        position = next((asset for asset in margin_account['assets'] if asset['symbol'] == symbol), None)
        if not position:
            return False

        # Чистая позиция: baseAsset.netAsset (положительное значение — long, отрицательное — short)
        net_asset = Decimal(position['baseAsset']['netAsset'])

        if position_type == "long" and net_asset > 0:
            return True
        elif position_type == "short" and net_asset < 0:
            return True

        return False
    except Exception as e:
        print(Fore.RED + f"Ошибка проверки позиции для {symbol}: {str(e)}")
        return False


@app.route("/position", methods=["POST"])
def handle_position():
    data = request.json
    # Логирование полученного сообщения
    print(Fore.CYAN + f"Получено сообщение: {data}")
    
    try:
        action = data.get("action")
        symbol = data.get("symbol")
        margin = data.get("margin", 0)
        leverage = data.get("leverage", 1)
        position_type = data.get("position_type")

        print(Fore.YELLOW + "Обработанные данные:")
        print(Fore.YELLOW + f"Действие: {action}")
        print(Fore.YELLOW + f"Инструмент: {symbol}")
        print(Fore.YELLOW + f"Маржа: {margin}")
        print(Fore.YELLOW + f"Плечо: {leverage}")
        print(Fore.YELLOW + f"Тип позиции: {position_type}")

        # Проверка обязательных параметров
        if not all([action, symbol, position_type]):
            return jsonify({"status": "error", "message": "Отсутствуют обязательные параметры"}), 400

        # Логика обработки действия
        if action == "buy":
            if position_type == "flat":
                # Закрытие короткой позиции (short)
                return jsonify(close_position(symbol, margin, leverage, "short"))
            else:
                # Открытие длинной позиции (long)
                current_position = check_open_position(symbol, "short")
                if current_position:
                    print(Fore.YELLOW + f"Закрываем открытую SHORT позицию для {symbol}")
                    close_position(symbol, margin, leverage, "short")
                return jsonify(open_position(symbol, margin, leverage, "long"))

        elif action == "sell":
            if position_type == "flat":
                # Закрытие длинной позиции (long)
                return jsonify(close_position(symbol, margin, leverage, "long"))
            else:
                # Открытие короткой позиции (short)
                current_position = check_open_position(symbol, "long")
                if current_position:
                    print(Fore.YELLOW + f"Закрываем открытую LONG позицию для {symbol}")
                    close_position(symbol, margin, leverage, "long")
                return jsonify(open_position(symbol, margin, leverage, "short"))

        else:
            return jsonify({"status": "error", "message": "Некорректное действие"}), 400

    except Exception as e:
        # Логирование ошибок
        print(Fore.RED + f"Ошибка обработки запроса: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    public_url = ngrok.connect(8080, bind_tls=True)
    print(Fore.GREEN + f"Сервер доступен по адресу: {public_url}")
    app.run(port=8080, debug=False)

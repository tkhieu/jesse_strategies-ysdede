import numpy as np
from jesse import utils
from jesse.strategies import Strategy, cached
import talib

import custom_indicators as cta
from vars import tp_qtys
import json
from datetime import datetime

# Ott Bands strategy with fixed risk allocation
# Leverage must be 10x! You can adjust the risk allocation at self.buy and self.sell below - self.pos_size * 2
# Optimized with Optuna NSGA-II
# Optuna optimization results removed. See OB5F_LS
# Exchange rules have been added. For rounding concerns, minimum quantity, quantity precision, and quote precision are used.
# Added a mechanism to liquidate remaining quantities when all take profit points are reached to eliminate any potential live/paper trading issues.
# Added a method to update stoploss quantity when the position size is reduced!

class OB5F_LSv2(Strategy):
    def __init__(self):
        super().__init__()

        # DO NOT edit variables below. See settings section.
        self.rules = None
        self.quantityPrecision = 3
        self.quotePrecision = 8
        self.minQty = 0.01
        self.pricePrecision = 3
        self.run_once = True

        self.tps_hit_max = len(tp_qtys[0])
        self.tps_hit = 0

        self.trade_ts = None
        self.initial_qty = 0
        self.longs = 0
        self.shorts = 0
        
        self.cycle_sl = 0.0
        self.fib = (0.01, 0.02, 0.03, 0.05, 0.08)
        # Higher take profit at 8% helps to avoid re-entry at the peak.
        # Signal line crosses the upper band again and again.
        # self.fib = (0.01, 0.02, 0.03, 0.05, 0.065)

        # Set False after opening a long position to avoid re-opening a new long position.
        # Set True after ott long signal (mavg) crosses down the ott line.
        
        self.ott_long_reset = True
        self.prev_balance = 0

        # Settings:
        self.log_enabled = True

        # self.hp_l = {'ott_len': 37, 'ott_percent': 129, 'ott_bw_up': 111, 'tps_qty_index': 42, 'max_risk_long': 65}
        # self.hp_l = {'ott_len': 33, 'ott_percent': 129, 'ott_bw_up': 111, 'tps_qty_index': 65, 'max_risk_long': 85}
        # self.hp_l = {'ott_len': 33, 'ott_percent': 129, 'ott_bw_up': 112, 'tps_qty_index': 106, 'max_risk_long': 52}
        # self.hp_l = {'ott_len': 33, 'ott_percent': 129, 'ott_bw_up': 111, 'tps_qty_index': 61, 'max_risk_long': 81}
        # self.hp_l = {'ott_len': 33, 'ott_percent': 129, 'ott_bw_up': 148, 'tps_qty_index': 110, 'max_risk_long': 55}
        self.hp_l = {'ott_len': 70,
                     'ott_percent': 170,
                     'ott_bw_up': 135,
                     'tps_qty_index': 64,
                     'max_risk_long': 52}

        # 0523321840830300523
        # 0314021840210390763
        # 0325142510770340713
        # 0265242341240880743
        # 0204772181090990793
        # self.hp_s = {"ott_len": 20, "ott_percent": 477, "ott_bw_down": 218, "tps_qty_index": 51, "max_risk_short": 113, "signal_ma_len": 77}
        # self.hp_s = {"ott_len": 28, "ott_percent": 477, "ott_bw_down": 244, "tps_qty_index": 116, "max_risk_short": 75, "signal_ma_len": 59}
        # self.hp_s = {"ott_len": 26, "ott_percent": 524, "ott_bw_down": 234, "tps_qty_index": 124, "max_risk_short": 88, "signal_ma_len": 74}
        # self.hp_s = {"ott_len": 20, "ott_percent": 477, "ott_bw_down": 218, "tps_qty_index": 109, "max_risk_short": 99, "signal_ma_len": 79}
        self.hp_s = {"ott_len": 31,
                     "ott_percent": 333,
                     "ott_bw_down": 184,
                     "tps_qty_index": 51,
                     "max_risk_short": 46,
                     "signal_ma_len": 59}

    def first_run(self):
        try:
            with open('BinanceFuturesExchangeInfo.json') as f:
                data = json.load(f)

            for i in data['symbols']:
                if i['symbol'] == self.symbol.replace('-', ''):
                    self.rules = i
        except:
            self.console("BinanceFuturesExchangeInfo.json not found")
            exit()

        if self.rules:
            self.quantityPrecision = int(self.rules['quantityPrecision'])
            self.quotePrecision = int(self.rules['quotePrecision'])
            self.pricePrecision = int(self.rules['pricePrecision'])
            self.minQty = float(self.rules['filters'][1]['minQty'])
            self.console(f"Rules set - quantityPrecision:{self.quantityPrecision}, minQty:{self.minQty}, quotePrecision:{self.quotePrecision}, pricePrecision:{self.pricePrecision}")

        # print(f"\n{self.symbol} - {self.tps_hit_max=}")

        self.prev_balance = self.balance
        self.run_once = False

    @property
    def ott_len_l(self):
        return self.hp_l['ott_len']

    @property
    def ott_percent_l(self):
        return self.hp_l['ott_percent'] / 100

    @property
    def ott_len_s(self):
        return self.hp_s['ott_len']

    @property
    def ott_percent_s(self):
        return self.hp_s['ott_percent'] / 100

    @property
    def max_risk_long(self):
        return self.hp_l['max_risk_long'] / 10

    @property
    def max_risk_short(self):
        return self.hp_s['max_risk_short'] / 10

    @property
    @cached
    def ott_l(self):
        return cta.ott(self.candles[-240:, 2], self.ott_len_l, self.ott_percent_l, ma_type='kama', sequential=True)

    @property
    @cached
    def ott_s(self):
        return cta.ott(self.candles[-240:, 2], self.ott_len_s, self.ott_percent_s, ma_type='kama', sequential=True)

    @property
    @cached
    def ott_upper_band(self):
        multiplier = 1 + round((self.hp_l['ott_bw_up'] / 10000), 4)
        return np.multiply(self.ott_l.ott, multiplier)

    @property
    def cross_up_upper_band(self):
        return utils.crossed(self.ott_l.mavg, self.ott_upper_band, direction='above', sequential=False)

    @property
    def cross_down_upper_band(self):
        return utils.crossed(self.ott_l.mavg, self.ott_upper_band, direction='below', sequential=False)

    @property
    def cross_up_l(self):
        return utils.crossed(self.ott_l.mavg, self.ott_l.ott, direction='above', sequential=False)

    @property
    def cross_down_l(self):
        return utils.crossed(self.ott_l.mavg, self.ott_l.ott, direction='below', sequential=False)

    @property
    @cached
    def signal_ma_s(self):  # Just for short
        return talib.KAMA(self.candles[-240:, 2], self.hp_s['signal_ma_len'])

    @property
    def cross_up_s(self):
        return utils.crossed(self.signal_ma_s, self.ott_s.ott, direction='above', sequential=False)

    @property
    def cross_down_s(self):
        return utils.crossed(self.signal_ma_s, self.ott_s.ott, direction='below', sequential=False)

    @property
    @cached
    def ott_lower_band(self):
        multiplier = 1 - (self.hp_s['ott_bw_down'] / 10000)
        return np.multiply(self.ott_s.ott, multiplier)

    @property
    def cross_down_lower_band(self):
        return utils.crossed(self.signal_ma_s, self.ott_lower_band, direction='below', sequential=False)

    @property
    def cross_up_lower_band(self):
        return utils.crossed(self.signal_ma_s, self.ott_lower_band, direction='above', sequential=False)

    @property
    def calc_risk_for_long(self):
        sl = self.calc_long_stop
        margin_size = self.pos_size_in_usd * self.leverage
        margin_risk = margin_size * ((self.close - sl) / self.close)
        return margin_risk / self.capital * 100 <= self.max_risk_long

    @property
    def calc_risk_for_short(self):
        sl = self.calc_short_stop
        margin_size = self.pos_size_in_usd * self.leverage
        margin_risk = margin_size * ((abs(self.close - sl)) / self.close)
        return margin_risk / self.capital * 100 <= self.max_risk_short

    def should_long(self) -> bool:
        return self.cross_up_upper_band and self.calc_risk_for_long # and self.ott_long_reset

    def should_short(self) -> bool:
        return self.cross_down_lower_band and self.calc_risk_for_short

    @property
    def pos_size_in_usd(self):
        return self.capital / 10

    @property
    def pos_size(self):
        qty = round(utils.size_to_qty(self.pos_size_in_usd, self.price, precision=self.quantityPrecision, fee_rate=self.fee_rate) * self.leverage, self.quantityPrecision)
        return max(self.minQty, qty)

    def go_long(self):
        self.buy = self.pos_size, round(self.price, self.pricePrecision)

    def go_short(self):
        self.sell = self.pos_size, round(self.price, self.pricePrecision)

    @property
    def calc_long_stop(self):
        return round(self.ott_l.ott[-1], self.pricePrecision)

    @property
    def calc_short_stop(self):
        return round(self.ott_s.ott[-1], self.pricePrecision)

    def on_open_position(self, order):
        self.prev_balance = self.balance
        self.tps_hit = 0    # TODO Use reduced count!!!
        qty = self.position.qty
        share = self.position.qty / 10
        tps = []

        if self.is_long:
            self.ott_long_reset = False
            side = 'Long'
            self.longs += 1
            sl = self.calc_long_stop
            self.cycle_sl = sl

            for i in range(self.tps_hit_max):
                p = round(self.position.entry_price * (1 + self.fib[i]), self.pricePrecision)
                q = round(tp_qtys[self.hp_l['tps_qty_index']][i] * share, self.quantityPrecision + 1)
                tps.append((q, p))

        if self.is_short:
            side = 'Short'
            self.shorts += 1
            sl = self.calc_short_stop

            for i in range(self.tps_hit_max):
                p = round(self.position.entry_price * (1 - self.fib[i]), self.pricePrecision)
                q = round(tp_qtys[self.hp_s['tps_qty_index']][i] * share, self.quantityPrecision + 1)
                tps.append((q, p))

        tp4_validation = round(qty - (tps[0][0] + tps[1][0] + tps[2][0] + tps[3][0]), self.quantityPrecision + 1)
        qty_validation = round(tps[0][0] + tps[1][0] + tps[2][0] + tps[3][0] + tps[4][0], self.quantityPrecision + 1)

        self.console(f"{side}, Entry: {self.position.entry_price:0.2f}, SL: {sl}, ott_l: {self.ott_l.ott[-1]:0.02f}, tps: {tps} {tp4_validation=}, {qty=} {qty_validation=}")

        if qty_validation != qty:
            self.console(f'{side} QTY != Sum(qtys) {qty}!={qty_validation}' * 4)

        if tp4_validation != tps[4][0]:
            self.console(f'{side} tp4 qty != validation tp4 qty {tps[4][0]}!={tp4_validation} !')

        self.cycle_sl = sl
        self.stop_loss = qty, sl
        self.take_profit = tps
        self.initial_qty = self.position.qty
        self.tps = tps

    def on_reduced_position(self, order) -> None:
        pnl = self.balance - self.prev_balance
        self.prev_balance = self.balance
        self.tps_hit += 1
        self.stop_loss = self.position.qty, self.cycle_sl
        self.console(f'✂ Reduced. Tps hit: {self.tps_hit}/{self.tps_hit_max}, Tp: {self.tps[self.tps_hit-1]}, Price: {self.price:0.02f}, Remaining Qty: {self.position.qty}, Entry: {self.position.entry_price}, Starting Sl: {self.cycle_sl}, Pnl: {pnl:0.2f}')

    def on_close_position(self, order):
        # self.tps_hit += 1
        pnl = self.balance - self.prev_balance
        self.prev_balance = self.balance
        self.console(f"🔒 Closed position. Price: {self.price:0.02f}, Pnl: {pnl:0.2f}, Tps hit: {self.tps_hit}/{self.tps_hit_max}")

    def update_position(self):

        if self.is_long and self.cross_down_l:
            self.console(f"❌ Closing Long Position by cross down l. Tps hit: {self.tps_hit}/{self.tps_hit_max}, Remaining qty: {self.position.qty}, PNL: {self.position.pnl:0.02f}")
            self.liquidate()

        if self.is_short and self.cross_up_s:
            self.console(f"❌ Closing Short Position by cross up s. Tps hit: {self.tps_hit}/{self.tps_hit_max}, Remaining qty: {self.position.qty}, PNL: {self.position.pnl:0.02f}")
            self.liquidate()

        # Probably unnecessary Kill switch
        if self.tps_hit >= self.tps_hit_max:
            self.liquidate()
            self.console(f'🌊 Kill Switch: {self.tps_hit_max} !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')

    def watch_list(self):
        return [
            ('Symbol', self.symbol),
            ('Longs', self.longs),
            ('Shorts', self.shorts)
        ]

    def terminate(self):
        ratio = round(self.longs / (self.longs + self.shorts), 2) if self.longs > 0 else 0
        print(f"{self.symbol} longs:{self.longs}, shorts:{self.shorts}, total:{self.longs + self.shorts}, ratio:{ratio}")

    def should_cancel(self) -> bool:
        return True

    def before(self) -> None:
        if self.run_once:
            self.first_run()
        
        if self.cross_down_l:
            self.ott_long_reset = True

    def console(self, msg):
        if self.log_enabled:
            ts = datetime.utcfromtimestamp(self.current_candle[0]/1000).strftime('%Y-%m-%d %H:%M:%S')
            print(f'\n{ts} {self.symbol} {msg}')

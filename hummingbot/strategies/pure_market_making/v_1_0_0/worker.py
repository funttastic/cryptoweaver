from logging import DEBUG, INFO, WARNING
from os import path

import asyncio
import copy
import math
import numpy as np
import time
import traceback
from decimal import Decimal
from dotmap import DotMap
from pathlib import Path
from typing import Any, List, Union, Dict

from hummingbot.constants import DECIMAL_NAN
from hummingbot.constants import KUJIRA_NATIVE_TOKEN, DECIMAL_ZERO, VWAP_THRESHOLD, \
	FLOAT_ZERO, FLOAT_INFINITY
from hummingbot.gateway import Gateway
from hummingbot.strategies.worker_base import WorkerBase
from hummingbot.types import OrderSide, OrderType, Order, OrderStatus, \
	MiddlePriceStrategy, PriceStrategy
from hummingbot.utils import current_timestamp
from utils import dump


class Worker(WorkerBase):

	def __init__(self, parent, configuration):
		try:
			self._parent = parent
			self.id = f"""{self._parent.id}:worker:{configuration.id}"""
			self.logger_prefix = self.id

			self.log(DEBUG, "start")

			super().__init__()

			self._can_run: bool = True
			self._script_name = path.basename(Path(__file__))
			self._configuration = DotMap(configuration, _dynamic=False)
			self._wallet_address = None
			self._quote_token_name = None
			self._base_token_name = None
			self._is_busy: bool = False
			self._refresh_timestamp: int = 0
			self._market: DotMap[str, Any]
			self._balances: DotMap[str, Any] = DotMap({}, _dynamic=False)
			self._tickers: DotMap[str, Any]
			self._currently_tracked_orders_ids: [str] = []
			self._tracked_orders_ids: [str] = []
			self._open_orders: DotMap[str, Any]
			self._filled_orders: DotMap[str, Any]
			self._tasks: DotMap[str, asyncio.Task] = DotMap({
				"on_tick": None,
				"markets": None,
				"order_books": None,
				"tickers": None,
				"orders": None,
				"balances": None,
			}, _dynamic=False)
			self.summary: DotMap[str, Any] = DotMap({
				"orders": {
					"replaced": DotMap({}),
					"canceled": DotMap({}),
				},
				"wallet": {
					"current_initial_pnl": None,
					"initial_value": None,
					"previous_value": None,
					"current_value": None,
					"current_previous_pnl": None,
				},
				"token": {
					"initial_price": None,
					"previous_price": None,
					"current_price": None,
					"current_initial_pnl": None,
					"current_previous_pnl": None,
				},
				"price": {
					"used_price": None,
					"ticker_price": None,
					"last_filled_order_price": None,
					"expected_price": None,
					"adjusted_market_price": None,
					"sap": None,
					"wap": None,
					"vwap": None,
				},
				"balance": {
					"wallet": {
						"base": None,
						"quote": None,
						"WRAPPED_SOL": None,
						"UNWRAPPED_SOL": None,
						"ALL_SOL": None,
					},
					"orders": {
						"quote": {
							"bids": None,
							"asks": None,
							"total": None,
						}
					}
				}
			})
		finally:
			self.log(DEBUG, "end")

	# noinspection PyAttributeOutsideInit
	async def initialize(self):
		try:
			self.log(DEBUG, "start")

			self.initialized = False

			self._market_name = self._configuration.market

			self._wallet_address = self._configuration.wallet

			self._market = await self._get_market()

			self._base_token = self._market.baseToken
			self._quote_token = self._market.quoteToken

			self._base_token_name = self._market.baseToken.name
			self._quote_token_name = self._market.quoteToken.name

			if self._configuration.strategy.cancel_all_orders_on_start:
				try:
					await self._cancel_all_orders()
				except Exception as exception:
					self.ignore_exception(exception)

			if self._configuration.strategy.withdraw_market_on_start:
				try:
					await self._market_withdraw()
				except Exception as exception:
					self.ignore_exception(exception)

			waiting_time = self._calculate_waiting_time(self._configuration.strategy.tick_interval)
			self.log(DEBUG, f"""Waiting for {waiting_time}s.""")
			self._refresh_timestamp = waiting_time + current_timestamp()

			self.initialized = True
			self._can_run = True
		except Exception as exception:
			self.ignore_exception(exception)

			await self.exit()
		finally:
			self.log(DEBUG, "end")

	async def start(self):
		await self.initialize()

		while self._can_run:
			if (not self._can_run) and (not self._is_busy):
				await self.exit()

			if self._is_busy or (self._refresh_timestamp > current_timestamp()):
				continue

			if self._tasks.on_tick is None:
				try:
					self._tasks.on_tick = asyncio.create_task(self.on_tick())
					await self._tasks.on_tick
				finally:
					self._tasks.on_tick = None

	async def stop(self):
		try:
			self.log(DEBUG, "start")

			self._can_run = False

			try:
				self._tasks.on_tick.cancel()
				await self._tasks.on_tick
			except Exception as exception:
				self.ignore_exception(exception)
			finally:
				self._tasks.on_tick = None

			if self._configuration.strategy.cancel_all_orders_on_stop:
				try:
					await self._cancel_all_orders()
				except Exception as exception:
					self.ignore_exception(exception)

			if self._configuration.strategy.withdraw_market_on_stop:
				try:
					await self._market_withdraw()
				except Exception as exception:
					self.ignore_exception(exception)
		finally:
			await self.exit()

			self.log(DEBUG, "end")

	async def exit(self):
		self.log(DEBUG, "start")
		self.log(DEBUG, "end")

	async def on_tick(self):
		try:
			self.log(DEBUG, "start")

			self._is_busy = True

			if self._configuration.strategy.withdraw_market_on_tick:
				try:
					await self._market_withdraw()
				except Exception as exception:
					self.ignore_exception(exception)

			open_orders = await self._get_open_orders(use_cache=False)
			await self._get_filled_orders(use_cache=False)
			await self._get_balances(use_cache=False)

			open_orders_ids = list(open_orders.keys())
			await self._cancel_currently_untracked_orders(open_orders_ids)

			proposed_orders: List[Order] = await self._create_proposal()
			candidate_orders: List[Order] = await self._adjust_proposal_to_budget(proposed_orders)

			await self._replace_orders(candidate_orders)
		except Exception as exception:
			self.ignore_exception(exception)
		finally:
			waiting_time = self._calculate_waiting_time(self._configuration.strategy.tick_interval)

			self._refresh_timestamp = waiting_time + current_timestamp()
			self._is_busy = False

			self.log(DEBUG, f"""Waiting for {waiting_time}s.""")

			self.log(DEBUG, "end")

			if self._configuration.strategy.run_only_once:
				await self.exit()

	async def _create_proposal(self) -> List[Order]:
		try:
			self.log(DEBUG, "start")

			order_book = await self._get_order_book()
			bids, asks = self._parse_order_book(order_book)

			ticker_price = await self._get_market_price()
			try:
				last_filled_order_price = await self._get_last_filled_order_price()
			except Exception as exception:
				self.ignore_exception(exception)

				last_filled_order_price = DECIMAL_ZERO

			price_strategy = PriceStrategy[self._configuration.strategy.get("price_strategy", PriceStrategy.TICKER.name)]
			if price_strategy == PriceStrategy.TICKER:
				used_price = ticker_price
			elif price_strategy == PriceStrategy.MIDDLE:
				middle_price_strategy = MiddlePriceStrategy[self._configuration.strategy.get("middle_price_strategy", MiddlePriceStrategy.SAP.name)]

				used_price = await self._get_market_middle_price(
					bids,
					asks,
					middle_price_strategy
				)
			elif price_strategy == PriceStrategy.LAST_FILL:
				used_price = last_filled_order_price
			else:
				raise ValueError("""Invalid "strategy.middle_price_strategy" configuration value.""")

			if used_price is None or used_price <= DECIMAL_ZERO:
				raise ValueError(f"Invalid price: {used_price}")

			minimum_price_increment = Decimal(self._market.minimumPriceIncrement)
			minimum_order_size = Decimal(self._market.minimumOrderSize)

			client_id = 1
			proposal = []

			bid_orders = []
			for index, layer in enumerate(self._configuration.strategy.layers, start=1):
				best_ask = Decimal(next(iter(asks), {"price": FLOAT_INFINITY}).price)
				bid_quantity = int(layer.bid.quantity)
				bid_spread_percentage = Decimal(layer.bid.spread_percentage)
				bid_market_price = ((100 - bid_spread_percentage) / 100) * min(used_price, best_ask)
				bid_max_liquidity_in_dollars = Decimal(layer.bid.max_liquidity_in_dollars)
				bid_size = bid_max_liquidity_in_dollars / bid_market_price / bid_quantity if bid_quantity > 0 else 0

				if bid_market_price < minimum_price_increment:
					self.log(
						WARNING,
						f"""Skipping orders placement from layer {index}, bid price too low:\n\n{'{:^30}'.format(round(bid_market_price, 6))}"""
					)
				elif bid_size < minimum_order_size:
					self.log(
						WARNING,
						f"""Skipping orders placement from layer {index}, bid size too low:\n\n{'{:^30}'.format(round(bid_size, 9))}"""
					)
				else:
					for i in range(bid_quantity):
						bid_order = Order()
						bid_order.client_id = str(client_id)
						bid_order.market_name = self._market_name
						bid_order.type = OrderType.LIMIT
						bid_order.side = OrderSide.BUY
						bid_order.amount = bid_size
						bid_order.price = bid_market_price

						bid_orders.append(bid_order)

						client_id += 1

			ask_orders = []
			for index, layer in enumerate(self._configuration.strategy.layers, start=1):
				best_bid = Decimal(next(iter(bids), {"price": FLOAT_ZERO}).price)
				ask_quantity = int(layer.ask.quantity)
				ask_spread_percentage = Decimal(layer.ask.spread_percentage)
				ask_market_price = ((100 + ask_spread_percentage) / 100) * max(used_price, best_bid)
				ask_max_liquidity_in_dollars = Decimal(layer.ask.max_liquidity_in_dollars)
				ask_size = ask_max_liquidity_in_dollars / ask_market_price / ask_quantity if ask_quantity > 0 else 0

				if ask_market_price < minimum_price_increment:
					self.log(
						WARNING,
						f"""Skipping orders placement from layer {index}, ask price too low:\n\n{'{:^30}'.format(round(ask_market_price, 9))}""",
						True
					)
				elif ask_size < minimum_order_size:
					self.log(
						WARNING,
						f"""Skipping orders placement from layer {index}, ask size too low:\n\n{'{:^30}'.format(round(ask_size, 9))}""",
						True
					)
				else:
					for i in range(ask_quantity):
						ask_order = Order()
						ask_order.client_id = str(client_id)
						ask_order.market_name = self._market_name
						ask_order.type = OrderType.LIMIT
						ask_order.side = OrderSide.SELL
						ask_order.amount = ask_size
						ask_order.price = ask_market_price

						ask_orders.append(ask_order)

						client_id += 1

			proposal = [*proposal, *bid_orders, *ask_orders]

			self.log(DEBUG, f"""proposal:\n{dump(proposal)}""")

			return proposal
		finally:
			self.log(DEBUG, "end")

	async def _adjust_proposal_to_budget(self, candidate_proposal: List[Order]) -> List[Order]:
		try:
			self.log(DEBUG, "start")

			adjusted_proposal: List[Order] = []

			balances = await self._get_balances()
			base_balance = Decimal(balances.tokens[self._base_token.id].free)
			quote_balance = Decimal(balances.tokens[self._quote_token.id].free)
			current_base_balance = base_balance
			current_quote_balance = quote_balance

			for order in candidate_proposal:
				if order.side == OrderSide.BUY:
					if current_quote_balance > order.amount:
						current_quote_balance -= order.amount
						adjusted_proposal.append(order)
					else:
						continue
				elif order.side == OrderSide.SELL:
					if current_base_balance > order.amount:
						current_base_balance -= order.amount
						adjusted_proposal.append(order)
					else:
						continue
				else:
					raise ValueError(f"""Unrecognized order size "{order.side}".""")

			self.log(DEBUG, f"""adjusted_proposal:\n{dump(adjusted_proposal)}""")

			return adjusted_proposal
		finally:
			self.log(DEBUG, "end")

	async def _get_base_ticker_price(self) -> Decimal:
		try:
			self.log(DEBUG, "start")

			return Decimal((await self._get_ticker(use_cache=False)).price)
		finally:
			self.log(DEBUG, "end")

	async def _get_last_filled_order_price(self) -> Decimal:
		try:
			self.log(DEBUG, "start")

			last_filled_order = await self._get_last_filled_order()

			if last_filled_order:
				return Decimal(last_filled_order.price)
			else:
				return DECIMAL_NAN
		finally:
			self.log(DEBUG, "end")

	async def _get_market_price(self) -> Decimal:
		return await self._get_base_ticker_price()

	async def _get_market_middle_price(self, bids, asks, strategy: MiddlePriceStrategy = None) -> Decimal:
		try:
			self.log(DEBUG, "start")

			if strategy:
				return self._calculate_middle_price(bids, asks, strategy)

			try:
				return self._calculate_middle_price(bids, asks, MiddlePriceStrategy.VWAP)
			except (Exception,):
				try:
					return self._calculate_middle_price(bids, asks, MiddlePriceStrategy.WAP)
				except (Exception,):
					try:
						return self._calculate_middle_price(bids, asks, MiddlePriceStrategy.SAP)
					except (Exception,):
						return await self._get_market_price()
		finally:
			self.log(DEBUG, "end")

	async def _get_base_balance(self) -> Decimal:
		try:
			self.log(DEBUG, "start")

			base_balance = Decimal((await self._get_balances())[self._base_token.id].free)

			return base_balance
		finally:
			self.log(DEBUG, "end")

	async def _get_quote_balance(self) -> Decimal:
		try:
			self.log(DEBUG, "start")

			quote_balance = Decimal((await self._get_balances())[self._quote_token.id].free)

			return quote_balance
		finally:
			self.log(DEBUG, "start")

	async def _get_balances(self, use_cache: bool = True) -> DotMap[str, Any]:
		try:
			self.log(DEBUG, "start")

			response = None
			try:
				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"ownerAddress": self._wallet_address,
					"tokenIds": [KUJIRA_NATIVE_TOKEN.id, self._base_token.id, self._quote_token.id]
				}

				self.log(INFO, f"""gateway.kujira_get_balances: request:\n{dump(request)}""")

				if use_cache and self._balances is not None:
					response = self._balances
				else:
					response = await Gateway.kujira_get_balances(request)

					self._balances = DotMap(copy.deepcopy(response), _dynamic=False)

					self._balances.total.free = Decimal(self._balances.total.free)
					self._balances.total.lockedInOrders = Decimal(self._balances.total.lockedInOrders)
					self._balances.total.unsettled = Decimal(self._balances.total.unsettled)

					for (token, balance) in DotMap(response.tokens).items():
						balance.free = Decimal(balance.free)
						balance.lockedInOrders = Decimal(balance.lockedInOrders)
						balance.unsettled = Decimal(balance.unsettled)

				return response
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(INFO, f"""gateway.kujira_get_balances: response:\n{dump(response)}""")
		finally:
			self.log(DEBUG, "end")

	async def _get_market(self):
		try:
			self.log(DEBUG, "start")

			request = None
			response = None
			try:
				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"name": self._market_name
				}

				self.log(
					INFO,
					f"""gateway.kujira_get_market: request:\n{dump(request)}"""
				)

				response = await Gateway.kujira_get_market(request)

				return response
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(
					INFO,
					f"""gateway.kujira_get_market: response:\n{dump(response)}"""
				)
		finally:
			self.log(DEBUG, "end")

	async def _get_order_book(self):
		try:
			self.log(DEBUG, "start")

			request = None
			response = None
			try:
				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"marketId": self._market.id
				}

				self.log(
					DEBUG,
					f"""gateway.kujira_get_order_books: request:\n{dump(request)}"""
				)

				response = await Gateway.kujira_get_order_book(request)

				return response
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(
					DEBUG,
					f"""gateway.kujira_get_order_books: response:\n{dump(response)}"""
				)
		finally:
			self.log(DEBUG, "end")

	async def _get_ticker(self, use_cache: bool = True) -> DotMap[str, Any]:
		try:
			self.log(DEBUG, "start")

			request = None
			response = None
			try:
				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"marketId": self._market.id
				}

				self.log(
					INFO,
					f"""gateway.kujira_get_ticker: request:\n{dump(request)}"""
				)

				if use_cache and self._tickers is not None:
					response = self._tickers
				else:
					response = await Gateway.kujira_get_ticker(request)

					self._tickers = response

				return response
			except Exception as exception:
				response = exception

				raise exception
			finally:
				self.log(
					INFO,
					f"""gateway.kujira_get_ticker: response:\n{dump(response)}"""
				)

		finally:
			self.log(DEBUG, "end")

	async def _get_open_orders(self, use_cache: bool = True) -> DotMap[str, Any]:
		try:
			self.log(DEBUG, "start")

			request = None
			response = None
			try:
				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"marketId": self._market.id,
					"ownerAddress": self._wallet_address,
					"status": OrderStatus.OPEN.value[0]
				}

				self.log(
					INFO,
					f"""gateway.kujira_get_open_orders: request:\n{dump(request)}"""
				)

				if use_cache and self._open_orders is not None:
					response = self._open_orders
				else:
					response = await Gateway.kujira_get_orders(request)
					self._open_orders = response

				return response
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(
					INFO,
					f"""gateway.kujira_get_open_orders: response:\n{dump(response)}"""
				)
		finally:
			self.log(DEBUG, "end")

	async def _get_last_filled_order(self) -> DotMap[str, Any]:
		try:
			self.log(DEBUG, "start")

			filled_orders = await self._get_filled_orders()

			if len((filled_orders or {})):
				last_filled_order = list(DotMap(filled_orders).values())[0]
			else:
				last_filled_order = None

			return last_filled_order
		finally:
			self.log(DEBUG, "end")

	async def _get_filled_orders(self, use_cache: bool = True) -> DotMap[str, Any]:
		try:
			self.log(DEBUG, "start")

			request = None
			response = None
			try:
				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"marketId": self._market.id,
					"ownerAddress": self._wallet_address,
					"status": OrderStatus.FILLED.value[0]
				}

				self.log(
					DEBUG,
					f"""gateway.kujira_get_filled_orders: request:\n{dump(request)}"""
				)

				if use_cache and self._filled_orders is not None:
					response = self._filled_orders
				else:
					response = await Gateway.kujira_get_orders(request)
					self._filled_orders = response

				return response
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(
					DEBUG,
					f"""gateway.kujira_get_filled_orders: response:\n{dump(response)}"""
				)

		finally:
			self.log(DEBUG, "end")

	async def _replace_orders(self, proposal: List[Order]) -> DotMap[str, Any]:
		try:
			self.log(DEBUG, "start")

			response = None
			try:
				orders = []
				for candidate in proposal:
					order_type = OrderType[self._configuration.strategy.get("kujira_order_type", OrderType.LIMIT.name)]

					orders.append({
						"clientId": candidate.client_id,
						"marketId": self._market.id,
						"ownerAddress": self._wallet_address,
						"side": candidate.side.value[0],
						"price": str(candidate.price),
						"amount": str(candidate.amount),
						"type": order_type.value[0],
					})

				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"orders": orders
				}

				self.log(INFO, f"""gateway.kujira_post_orders: request:\n{dump(request)}""")

				if len(orders):
					response = await Gateway.kujira_post_orders(request)

					self._currently_tracked_orders_ids = list(response.keys())
					self._tracked_orders_ids.extend(self._currently_tracked_orders_ids)
				else:
					self.log(WARNING, "No order was defined for placement/replacement. Skipping.", True)
					response = []

				return response
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(INFO, f"""gateway.kujira_post_orders: response:\n{dump(response)}""")
		finally:
			self.log(DEBUG, "end")

	async def _cancel_currently_untracked_orders(self, open_orders_ids: List[str]):
		try:
			self.log(DEBUG, "start")

			request = None
			response = None
			try:
				untracked_orders_ids = list(
					set(self._tracked_orders_ids).intersection(set(open_orders_ids)) - set(self._currently_tracked_orders_ids))

				if len(untracked_orders_ids) > 0:
					request = {
						"chain": self._configuration.chain,
						"network": self._configuration.network,
						"connector": self._configuration.connector,
						"ids": untracked_orders_ids,
						"marketId": self._market.id,
						"ownerAddress": self._wallet_address,
					}

					self.log(
						INFO,
						f"""gateway.kujira_delete_orders: request:\n{dump(request)}"""
					)

					response = await Gateway.kujira_delete_orders(request)
				else:
					self.log(INFO, "No order needed to be canceled.")
					response = {}

				return response
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(
					INFO,
					f"""gateway.kujira_delete_orders: response:\n{dump(response)}"""
				)
		finally:
			self.log(DEBUG, "end")

	async def _cancel_all_orders(self):
		try:
			self.log(DEBUG, "start")

			request = None
			response = None
			try:
				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"marketId": self._market.id,
					"ownerAddress": self._wallet_address,
				}

				self.log(
					INFO,
					f"""gateway.clob_delete_orders: request:\n{dump(request)}"""
				)

				response = await Gateway.kujira_delete_orders_all(request)
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(
					INFO,
					f"""gateway.clob_delete_orders: response:\n{dump(response)}"""
				)
		finally:
			self.log(DEBUG, "end")

	async def _market_withdraw(self):
		try:
			self.log(DEBUG, "start")

			response = None
			try:
				request = {
					"chain": self._configuration.chain,
					"network": self._configuration.network,
					"connector": self._configuration.connector,
					"marketId": self._market.id,
					"ownerAddress": self._wallet_address,
				}

				self.log(INFO, f"""gateway.kujira_post_market_withdraw: request:\n{dump(request)}""")

				response = await Gateway.kujira_post_market_withdraw(request)
			except Exception as exception:
				response = traceback.format_exc()

				raise exception
			finally:
				self.log(
					INFO,
					f"""gateway.kujira_post_market_withdraw: response:\n{dump(response)}"""
				)
		finally:
			self.log(DEBUG, "end")

	async def _get_remaining_orders_ids(self, candidate_orders, created_orders) -> List[str]:
		self.log(DEBUG, "end")

		try:
			candidate_orders_client_ids = [order.client_id for order in candidate_orders] if len(candidate_orders) else []
			created_orders_client_ids = [order.clientId for order in created_orders.values()] if len(
				created_orders) else []
			remaining_orders_client_ids = list(set(candidate_orders_client_ids) - set(created_orders_client_ids))
			remaining_orders_ids = list(
				filter(lambda order: (order.clientId in remaining_orders_client_ids), created_orders.values()))

			self.log(INFO, f"""remaining_orders_ids:\n{dump(remaining_orders_ids)}""")

			return remaining_orders_ids
		finally:
			self.log(DEBUG, "end")

	async def _get_duplicated_orders_ids(self) -> List[str]:
		self.log(DEBUG, "start")

		try:
			open_orders = (await self._get_open_orders()).values()

			open_orders_map = {}
			duplicated_orders_ids = []

			for open_order in open_orders:
				if open_order.clientId == "0":  # Avoid touching manually created orders.
					continue
				elif open_order.clientId not in open_orders_map:
					open_orders_map[open_order.clientId] = [open_order]
				else:
					open_orders_map[open_order.clientId].append(open_order)

			for orders in open_orders_map.values():
				orders.sort(key=lambda order: order.id)

				duplicated_orders_ids = [
					*duplicated_orders_ids,
					*[order.id for order in orders[:-1]]
				]

			self.log(INFO, f"""duplicated_orders_ids:\n{dump(duplicated_orders_ids)}""")

			return duplicated_orders_ids
		finally:
			self.log(DEBUG, "end")

	# noinspection PyMethodMayBeStatic
	def _parse_order_book(self, orderbook: DotMap[str, Any]) -> List[Union[List[DotMap[str, Any]], List[DotMap[str, Any]]]]:
		bids_list = []
		asks_list = []

		bids: DotMap[str, Any] = orderbook.bids
		asks: DotMap[str, Any] = orderbook.asks

		for value in bids.values():
			bids_list.append(DotMap({'price': value.price, 'amount': value.amount}, _dynamic=False))

		for value in asks.values():
			asks_list.append(DotMap({'price': value.price, 'amount': value.amount}, _dynamic=False))

		bids_list.sort(key=lambda x: x['price'], reverse=True)
		asks_list.sort(key=lambda x: x['price'], reverse=False)

		return [bids_list, asks_list]

	@staticmethod
	def _split_percentage(bids: [DotMap[str, Any]], asks: [DotMap[str, Any]]) -> List[Any]:
		asks = asks[:math.ceil((VWAP_THRESHOLD / 100) * len(asks))]
		bids = bids[:math.ceil((VWAP_THRESHOLD / 100) * len(bids))]

		return [bids, asks]

	# noinspection PyMethodMayBeStatic
	def _compute_volume_weighted_average_price(self, book: [DotMap[str, Any]]) -> np.array:
		prices = [float(order['price']) for order in book]
		amounts = [float(order['amount']) for order in book]

		prices = np.array(prices)
		amounts = np.array(amounts)

		vwap = (np.cumsum(amounts * prices) / np.cumsum(amounts))

		return vwap

	# noinspection PyMethodMayBeStatic
	def _remove_outliers(self, order_book: [DotMap[str, Any]], side: OrderSide) -> [DotMap[str, Any]]:
		prices = [order['price'] for order in order_book]

		q75, q25 = np.percentile(prices, [75, 25])

		# https://www.askpython.com/python/examples/detection-removal-outliers-in-python
		# intr_qr = q75-q25
		# max_threshold = q75+(1.5*intr_qr)
		# min_threshold = q75-(1.5*intr_qr) # Error: Sometimes this function assigns negative value for min

		max_threshold = q75 * 1.5
		min_threshold = q25 * 0.5

		orders = []
		if side == OrderSide.SELL:
			orders = [order for order in order_book if order['price'] < max_threshold]
		elif side == OrderSide.BUY:
			orders = [order for order in order_book if order['price'] > min_threshold]

		return orders

	def _calculate_middle_price(
		self,
		bids: [DotMap[str, Any]],
		asks: [DotMap[str, Any]],
		strategy: MiddlePriceStrategy
	) -> Decimal:
		if strategy == MiddlePriceStrategy.SAP:
			bid_prices = [float(item['price']) for item in bids]
			ask_prices = [float(item['price']) for item in asks]

			best_ask_price = 0
			best_bid_price = 0

			if len(ask_prices) > 0:
				best_ask_price = min(ask_prices)

			if len(bid_prices) > 0:
				best_bid_price = max(bid_prices)

			return Decimal((best_ask_price + best_bid_price) / 2.0)
		elif strategy == MiddlePriceStrategy.WAP:
			bid_prices = [float(item['price']) for item in bids]
			ask_prices = [float(item['price']) for item in asks]

			best_ask_price = 0
			best_ask_volume = 0
			best_bid_price = 0
			best_bid_amount = 0

			if len(ask_prices) > 0:
				best_ask_idx = ask_prices.index(min(ask_prices))
				best_ask_price = float(asks[best_ask_idx]['price'])
				best_ask_volume = float(asks[best_ask_idx]['amount'])

			if len(bid_prices) > 0:
				best_bid_idx = bid_prices.index(max(bid_prices))
				best_bid_price = float(bids[best_bid_idx]['price'])
				best_bid_amount = float(bids[best_bid_idx]['amount'])

			if best_ask_volume + best_bid_amount > 0:
				return Decimal(
					(best_ask_price * best_ask_volume + best_bid_price * best_bid_amount)
					/ (best_ask_volume + best_bid_amount)
				)
			else:
				return DECIMAL_ZERO
		elif strategy == MiddlePriceStrategy.VWAP:
			bids, asks = self._split_percentage(bids, asks)

			if len(bids) > 0:
				bids = self._remove_outliers(bids, OrderSide.BUY)

			if len(asks) > 0:
				asks = self._remove_outliers(asks, OrderSide.SELL)

			book = [*bids, *asks]

			if len(book) > 0:
				vwap = self._compute_volume_weighted_average_price(book)

				return Decimal(vwap[-1])
			else:
				return DECIMAL_ZERO
		else:
			raise ValueError(f'Unrecognized mid price strategy "{strategy}".')

	# noinspection PyMethodMayBeStatic
	def _calculate_waiting_time(self, number: int) -> int:
		current_timestamp_in_milliseconds = int(time.time() * 1000)
		result = number - (current_timestamp_in_milliseconds % number)

		return result


	def _show_summary(self):
		replaced_orders_summary = ""
		canceled_orders_summary = ""

		if len(self.summary["orders"]["replaced"]):
			orders: List[Dict[str, Any]] = list(dict(self.summary["orders"]["replaced"]).values())
			orders.sort(key=lambda item: item["price"])
	#
	#		groups: array[array[str]] = [[], [], [], [], [], [], []]
				# for order in orders:
				# groups[0].append(str(order["type"]).lower())
				# groups[1].append(str(order["side"]).lower())
				# groups[2].append(format_currency(order["amount"], 3))
				# groups[2].append(format_currency(order["amount"], 3))
				# groups[3].append(self._base_token)
				# groups[4].append("by")
				# groups[5].append(format_currency(order["price"], 3))
				# groups[5].append(format_currency(order["price"], 3))
				# groups[6].append(self._quote_token)
	#
	# 		replaced_orders_summary = format_lines(groups)
	#
	# 	if len(self.summary["orders"]["canceled"]):
	# 		orders: List[Dict[str, Any]] = list(dict(self.summary["orders"]["canceled"]).values())
	# 		orders.sort(key=lambda item: item["price"])
	#
	# 		groups: array[array[str]] = [[], [], [], [], [], []]
	# 		for order in orders:
	# 			groups[0].append(str(order["side"]).lower())
	# 			# groups[1].append(format_currency(order["amount"], 3))
	# 			groups[1].append(format_currency(order["amount"], 3))
	# 			groups[2].append(self._base_token)
	# 			groups[3].append("by")
	# 			# groups[4].append(format_currency(order["price"], 3))
	# 			groups[4].append(format_currency(order["price"], 3))
	# 			groups[5].append(self._quote_token)
	#
	# 		canceled_orders_summary = format_lines(groups)
	#
	# 	sot = self.configuration["strategy"].get("serum_order_type", "LIMIT")
	# 	ps = self.configuration["strategy"]["price_strategy"]
	# 	uap = self.configuration["strategy"]["use_adjusted_price"]
	# 	mps = self.configuration["strategy"].get("middle_price_strategy", "VWAP")
	#
	# 	self._log(
	# 		INFO,
	# 		textwrap.dedent(
	# 			f"""\
	#                 <b>Settings</b>:
	#                 {format_line("OrderType: ", sot)}\
	#                 {format_line("PriceStrategy: ", ps)}\
	#                 {format_line("UseAdjusted$: ", uap)}\
	#                 {format_line("Mid$Strategy: ", mps)}\
	#                 """
	# 		),
	# 		True
	# 	)
	#
	# 	self._log(
	# 		INFO,
	# 		textwrap.dedent(
	# 			f"""\
	#                 <b>Market</b>: <b>{self._market}</b>
	#                 <b>PnL</b>: {format_line("", format_percentage(self.summary["wallet"]["current_initial_pnl"]), alignment_column - 4)}
	#                 <b>Wallet</b>:
	#                 {format_line(" Wo:", format_currency(self.summary["wallet"]["initial_value"], 4))}
	#                 {format_line(" Wp:", format_currency(self.summary["wallet"]["previous_value"], 4))}
	#                 {format_line(" Wc:", format_currency(self.summary["wallet"]["current_value"], 4))}
	#                 {format_line(" Wc/Wo:", (format_percentage(self.summary["wallet"]["current_initial_pnl"])))}
	#                 {format_line(" Wc/Wp:", format_percentage(self.summary["wallet"]["current_previous_pnl"]))}
	#                 <b>Token</b>:
	#                 {format_line(" To:", format_currency(self.summary["token"]["initial_price"], 6))}
	#                 {format_line(" Tp:", format_currency(self.summary["token"]["previous_price"], 6))}
	#                 {format_line(" Tc:", format_currency(self.summary["token"]["current_price"], 6))}
	#                 {format_line(" Tc/To:", format_percentage(self.summary["token"]["current_initial_pnl"]))}
	#                 {format_line(" Tc/Tp:", format_percentage(self.summary["token"]["current_previous_pnl"]))}
	#                 <b>Price</b>:
	#                 {format_line(" Used:", format_currency(self.summary["price"]["used_price"], 6))}
	#                 {format_line(" External:", format_currency(self.summary["price"]["ticker_price"], 6))}
	#                 {format_line(" Last fill:", format_currency(self.summary["price"]["last_filled_order_price"], 6))}
	#                 {format_line(" Expected:", format_currency(self.summary["price"]["expected_price"], 6))}
	#                 {format_line(" Adjusted:", format_currency(self.summary["price"]["adjusted_market_price"], 6))}
	#                 {format_line(" SAP:", format_currency(self.summary["price"]["sap"], 6))}
	#                 {format_line(" WAP:", format_currency(self.summary["price"]["wap"], 6))}
	#                 {format_line(" VWAP:", format_currency(self.summary["price"]["vwap"], 6))}
	#                 <b>Balance</b>:
	#                  <b>Wallet</b>:
	#                 {format_line(f"  {self._base_token}:", format_currency(self.summary["balance"]["wallet"]["base"], 4))}
	#                 {format_line(f"  {self._quote_token}:", format_currency(self.summary["balance"]["wallet"]["quote"], 4))}
	#                 {format_line(f"  W SOL:", format_currency(self.summary["balance"]["wallet"]["WRAPPED_SOL"], 4))}
	#                 {format_line(f"  UW SOL:", format_currency(self.summary["balance"]["wallet"]["UNWRAPPED_SOL"], 4))}
	#                 {format_line(f"  ALL SOL:", format_currency(self.summary["balance"]["wallet"]["ALL_SOL"], 4))}
	#                  <b>Orders (in {self._quote_token})</b>:
	#                 {format_line(f"  Bids:", format_currency(self.summary["balance"]["orders"]["quote"]["bids"], 4))}
	#                 {format_line(f"  Asks:", format_currency(self.summary["balance"]["orders"]["quote"]["asks"], 4))}
	#                 {format_line(f"  Total:", format_currency(self.summary["balance"]["orders"]["quote"]["total"], 4))}
	#                 <b>Orders</b>:
	#                 {format_line(" Replaced:", str(len(self.summary["orders"]["replaced"])))}
	#                 {format_line(" Canceled:", str(len(self.summary["orders"]["canceled"])))}\
	#                 """
	# 		),
	# 		True
	# 	)
	#
	# 	if replaced_orders_summary:
	# 		self._log(
	# 			INFO,
	# 			f"""<b>Replaced Orders:</b>\n{replaced_orders_summary}""",
	# 			True
	# 		)
	#
	# 	if canceled_orders_summary:
	# 		self._log(
	# 			INFO,
	# 			f"""<b>Canceled Orders:</b>\n{canceled_orders_summary}""",
	# 			True
	# 		)

# def _show_summary(self):
# 	if len(self._summary["global"]["balances"]):
# 		groups: array[array[str]] = [[], []]
# 		for (token, balance) in dict(self._summary["global"]["balances"]).items():
# 			groups[0].append(token)
# 			# groups[1].append(format_currency(balance, 4))
# 			groups[1].append(format_currency(balance, 4))
#
# 		balances_summary = format_lines(groups, align="left")
# 	else:
# 		balances_summary = ""
#
# 	self._log(
# 		INFO,
# 		textwrap.dedent(
# 			f"""\
#                 <b>GLOBAL</b>
#                 <b>PnL</b>: {format_line("", format_percentage(self._summary["global"]["wallet"]["current_initial_pnl"]), alignment_column - 4)}
#                 <b>Wallet</b>:
#                 {format_line(" Wo:", format_currency(self._summary["global"]["wallet"]["initial_value"], 4))}
#                 {format_line(" Wp:", format_currency(self._summary["global"]["wallet"]["previous_value"], 4))}
#                 {format_line(" Wc:", format_currency(self._summary["global"]["wallet"]["current_value"], 4))}
#                 {format_line(" Wc/Wo:", (format_percentage(self._summary["global"]["wallet"]["current_initial_pnl"])))}
#                 {format_line(" Wc/Wp:", format_percentage(self._summary["global"]["wallet"]["current_previous_pnl"]))}\
#                 """
# 		),
# 		True
# 	)
#
# 	if balances_summary:
# 		self._log(
# 			INFO,
# 			f"""<b>Balances:</b>\n{balances_summary}""",
# 			True
# 		)
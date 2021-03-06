from _duel import ffi, lib
import os
import io
import struct
import random
import binascii
import pkgutil
import re
import datetime
import natsort

from . import callback_manager
from .card import Card
from .constants import *
from .duel_reader import DuelReader
from . import globals
from . import message_handlers

@ffi.def_extern()
def card_reader_callback(code, data):
	cd = data[0]
	row = globals.server.db.execute('select * from datas where id=?', (code,)).fetchone()
	cd.code = code
	cd.alias = row['alias']
	cd.setcode = row['setcode']
	cd.type = row['type']
	cd.level = row['level'] & 0xff
	cd.lscale = (row['level'] >> 24) & 0xff
	cd.rscale = (row['level'] >> 16) & 0xff
	cd.attack = row['atk']
	cd.defense = row['def']
	if cd.type & TYPE_LINK:
		cd.link_marker = cd.defense
		cd.defense = 0
	else:
		cd.link_marker = 0
	cd.race = row['race']
	cd.attribute = row['attribute']
	return 0

lib.set_card_reader(lib.card_reader_callback)

scriptbuf = ffi.new('char[131072]')
@ffi.def_extern()
def script_reader_callback(name, lenptr):
	fn = ffi.string(name)
	if not os.path.exists(fn):
		lenptr[0] = 0
		return ffi.NULL
	s = open(fn, 'rb').read()
	buf = ffi.buffer(scriptbuf)
	buf[0:len(s)] = s
	lenptr[0] = len(s)
	return ffi.cast('byte *', scriptbuf)

lib.set_script_reader(lib.script_reader_callback)

class Duel:
	def __init__(self, seed=None):
		self.buf = ffi.new('char[]', 4096)
		if seed is None:
			seed = random.randint(0, 0xffffffff)
		self.seed = seed
		self.duel = lib.create_duel(seed)
		lib.set_player_info(self.duel, 0, 8000, 5, 1)
		lib.set_player_info(self.duel, 1, 8000, 5, 1)
		self.cm = callback_manager.CallbackManager()
		self.keep_processing = False
		self.to_ep = False
		self.to_m2 = False
		self.current_phase = 0
		self.watchers = []
		self.private = False
		self.started = False
		self.debug_mode = False
		self.debug_fp = None
		self.players = [None, None]
		self.lp = [8000, 8000]
		self.started = False
		self.message_map = {}
		self.state = ''
		self.cards = [None, None]
		self.revealed = {}
		self.bind_message_handlers()

	def load_deck(self, player, cards, shuffle=True):
		self.cards[player] = cards[:]
		if shuffle:
			random.shuffle(self.cards[player])
		for c in self.cards[player][::-1]:
			lib.new_card(self.duel, c, player, player, LOCATION_DECK, 0, POS_FACEDOWN_DEFENSE);

	def start(self, options):
		if os.environ.get('DEBUG', 0):
			self.start_debug(options)
		lib.start_duel(self.duel, options)
		self.started = True

	def end(self):
		lib.end_duel(self.duel)
		self.started = False
		for pl in self.players + self.watchers:
			pl.duel = None
			pl.intercept = None
			if pl.connection is None:
				for opl in globals.server.get_all_players():
					opl.notify(opl._("%s logged out.")%(pl.nickname))
				globals.server.remove_player(pl.nickname)
			else:
				op = pl.connection.parser
				if isinstance(op, DuelReader):
					op.done = lambda caller: None
				pl.set_parser('LobbyParser')
				pl.watching = False
				pl.card_list = []
		for pl in self.watchers:
			pl.notify(pl._("Watching stopped."))
		if self.debug_mode is True and self.debug_fp is not None:
			self.debug_fp.close()
		globals.server.check_reboot()

	def process(self):
		res = lib.process(self.duel)
		l = lib.get_message(self.duel, ffi.cast('byte *', self.buf))
		data = ffi.unpack(self.buf, l)
		self.cm.call_callbacks('debug', event_type='process', result=res, data=data.decode('latin1'))
		data = self.process_messages(data)
		return res

	def process_messages(self, data):
		while data:
			msg = int(data[0])
			fn = self.message_map.get(msg)
			if fn:
				data = fn(data)
			else:
				print("msg %d unhandled" % msg)
				data = b''
		return data

	def read_cardlist(self, data, extra=False, extra8=False):
		res = []
		size = self.read_u8(data)
		for i in range(size):
			code = self.read_u32(data)
			controller = self.read_u8(data)
			location = self.read_u8(data)
			sequence = self.read_u8(data)
			card = self.get_card(controller, location, sequence)
			card.extra = 0
			if extra:
				if extra8:
					card.extra = self.read_u8(data)
				else:
					card.extra = self.read_u32(data)
			res.append(card)
		return res

	def read_u8(self, buf):
		return struct.unpack('b', buf.read(1))[0]

	def read_u16(self, buf):
		return struct.unpack('h', buf.read(2))[0]

	def read_u32(self, buf):
		return struct.unpack('I', buf.read(4))[0]

	def set_responsei(self, r):
		lib.set_responsei(self.duel, r)
		self.cm.call_callbacks('debug', event_type='set_responsei', response=r)

	def set_responseb(self, r):
		buf = ffi.new('char[64]', r)
		lib.set_responseb(self.duel, ffi.cast('byte *', buf))
		self.cm.call_callbacks('debug', event_type='set_responseb', response=r.decode('latin1'))

	def get_cards_in_location(self, player, location):
		cards = []
		flags = QUERY_CODE | QUERY_POSITION | QUERY_LEVEL | QUERY_RANK | QUERY_ATTACK | QUERY_DEFENSE | QUERY_EQUIP_CARD | QUERY_OVERLAY_CARD | QUERY_COUNTERS | QUERY_LINK
		bl = lib.query_field_card(self.duel, player, location, flags, ffi.cast('byte *', self.buf), False)
		buf = io.BytesIO(ffi.unpack(self.buf, bl))
		while True:
			if buf.tell() == bl:
				break
			length = self.read_u32(buf)
			if length == 4:
				continue #No card here
			f = self.read_u32(buf)
			code = self.read_u32(buf)
			card = Card(code)
			position = self.read_u32(buf)
			card.set_location(position)
			level = self.read_u32(buf)
			if (level & 0xff) > 0:
				card.level = level & 0xff
			rank = self.read_u32(buf)
			if (rank & 0xff) > 0:
				card.level = rank & 0xff
			card.attack = self.read_u32(buf)
			card.defense = self.read_u32(buf)

			card.equip_target = None

			if f & QUERY_EQUIP_CARD == QUERY_EQUIP_CARD: # optional

				equip_target = self.read_u32(buf)
				pl = equip_target & 0xff
				loc = (equip_target >> 8) & 0xff
				seq = (equip_target >> 16) & 0xff
				card.equip_target = self.get_card(pl, loc, seq)

			card.xyz_materials = []

			xyz = self.read_u32(buf)

			for i in range(xyz):
				card.xyz_materials.append(Card(self.read_u32(buf)))

			cs = self.read_u32(buf)
			card.counters = []
			for i in range(cs):
				card.counters.append(self.read_u32(buf))

			link = self.read_u32(buf)
			link_marker = self.read_u32(buf)

			if (link & 0xff) > 0:
				card.level = link & 0xff

			if link_marker > 0:
				card.defense = link_marker

			cards.append(card)
		return cards

	def get_card(self, player, loc, seq):
		flags = QUERY_CODE | QUERY_ATTACK | QUERY_DEFENSE | QUERY_POSITION | QUERY_LEVEL | QUERY_RANK | QUERY_LINK
		bl = lib.query_card(self.duel, player, loc, seq, flags, ffi.cast('byte *', self.buf), False)
		buf = io.BytesIO(ffi.unpack(self.buf, bl))
		f = self.read_u32(buf)
		if f == 4:
			return
		f = self.read_u32(buf)
		code = self.read_u32(buf)
		card = Card(code)
		position = self.read_u32(buf)
		card.set_location(position)
		level = self.read_u32(buf)
		if (level & 0xff) > 0:
			card.level = level & 0xff
		rank = self.read_u32(buf)
		if (rank & 0xff) > 0:
			card.level = rank & 0xff
		card.attack = self.read_u32(buf)
		card.defense = self.read_u32(buf)
		link = self.read_u32(buf)
		link_marker = self.read_u32(buf)
		if (link & 0xff) > 0:
			card.level = link & 0xff
		if link_marker > 0:
			card.defense = link_marker
		return card

	def unpack_location(self, loc):
		controller = loc & 0xff
		location = (loc >> 8) & 0xff
		sequence = (loc >> 16) & 0xff
		position = (loc >> 24) & 0xff
		return (controller, location, sequence, position)

	# all modules in ygo.message_handlers package will be imported here
	# if a module contains a MESSAGES dictionary attribute,
	# all of those entries will be considered message handlers
	# if a module contains a CALLBACKS dictionary attribute,
	# all of those entries will be considered callbacks for message handlers
	# all methods mentioned in those dictionaries will be linked into
	# the Duel object, same goes for all additional methods mentioned
	# in an additional METHODS dictionary attribute

	def bind_message_handlers(self):

		all_handlers = {}

		all_callbacks = {}

		all_methods = {}

		for importer, modname, ispkg in pkgutil.iter_modules(message_handlers.__path__):
			if not ispkg:
				try:
					m = importer.find_module(modname).load_module(modname)
					# check if we got message handlers registered in there
					handlers = m.__dict__.get('MESSAGES')
					if type(handlers) is dict:
						all_handlers.update(handlers)

					# process callbacks defined in there
					callbacks = m.__dict__.get('CALLBACKS')
					if type(callbacks) is dict:
						all_callbacks.update(callbacks)

					# additional methods we shell link?
					meths = m.__dict__.get('METHODS')
					if type(meths) is dict:
						all_methods.update(meths)

				except Exception as e:
					print("Error loading message handler", modname)
					print(e)

		# link all those methods into this object
		for h in all_handlers.keys():
			m = all_handlers[h].__get__(self)
			setattr(self, all_handlers[h].__name__, m)
			self.message_map[h] = m
		for c in all_callbacks.keys():
			m = all_callbacks[c].__get__(self)
			setattr(self, all_callbacks[c].__name__, m)
			self.cm.register_callback(c, m)
		for n in all_methods.keys():
			setattr(self, n, all_methods[n].__get__(self))

	def show_usable(self, pl):
		summonable = natsort.natsorted([card.get_spec(pl.duel_player) for card in self.summonable])
		spsummon = natsort.natsorted([card.get_spec(pl.duel_player) for card in self.spsummon])
		repos = natsort.natsorted([card.get_spec(pl.duel_player) for card in self.repos])
		mset = natsort.natsorted([card.get_spec(pl.duel_player) for card in self.idle_mset])
		idle_set = natsort.natsorted([card.get_spec(pl.duel_player) for card in self.idle_set])
		idle_activate = natsort.natsorted([card.get_spec(pl.duel_player) for card in self.idle_activate])
		if summonable:
			pl.notify(pl._("Summonable in attack position: %s") % ", ".join(summonable))
		if mset:
			pl.notify(pl._("Summonable in defense position: %s") % ", ".join(mset))
		if spsummon:
			pl.notify(pl._("Special summonable: %s") % ", ".join(spsummon))
		if idle_activate:
			pl.notify(pl._("Activatable: %s") % ", ".join(idle_activate))
		if repos:
			pl.notify(pl._("Repositionable: %s") % ", ".join(repos))
		if idle_set:
			pl.notify(pl._("Settable: %s") % ", ".join(idle_set))

	def cardspec_to_ls(self, text):
		if text.startswith('o'):
			text = text[1:]
		r = re.search(r'^([a-z]+)(\d+)', text)
		if not r:
			return (None, None)
		if r.group(1) == 'h':
			l = LOCATION_HAND
		elif r.group(1) == 'm':
			l = LOCATION_MZONE
		elif r.group(1) == 's':
			l = LOCATION_SZONE
		elif r.group(1) == 'g':
			l = LOCATION_GRAVE
		elif r.group(1) == 'x':
			l = LOCATION_EXTRA
		elif r.group(1) == 'r':
			l = LOCATION_REMOVED
		else:
			return None, None
		return l, int(r.group(2)) - 1

	def flag_to_usable_cardspecs(self, flag, reverse=False):
		pm = flag & 0xff
		ps = (flag >> 8) & 0xff
		om = (flag >> 16) & 0xff
		os = (flag >> 24) & 0xff
		zone_names = ('m', 's', 'om', 'os')
		specs = []
		for zn, val in zip(zone_names, (pm, ps, om, os)):
			for i in range(8):
				if reverse:
					avail = val & (1 << i) != 0
				else:
					avail = val & (1 << i) == 0
				if avail:
					specs.append(zn + str(i + 1))
		return specs

	def cardlist_info_for_player(self, card, pl):
		spec = card.get_spec(pl.duel_player)
		if card.location == LOCATION_DECK:
			spec = pl._("deck")
		cls = (card.controller, card.location, card.sequence)
		if card.controller != pl.duel_player and card.position in (0x8, 0xa) and cls not in self.revealed:
			position = card.get_position(pl)
			return (pl._("{position} card ({spec})")
				.format(position=position, spec=spec))
		name = card.get_name(pl)
		return "{name} ({spec})".format(name=name, spec=spec)

	def show_table(self, pl, player, hide_facedown=False):
		mz = self.get_cards_in_location(player, LOCATION_MZONE)
		sz = self.get_cards_in_location(player, LOCATION_SZONE)
		if len(mz+sz) == 0:
			pl.notify(pl._("Table is empty."))
			return
		for card in mz:
			s = "m%d: " % (card.sequence + 1)
			if hide_facedown and card.position in (0x8, 0xa):
				s += card.get_position(pl)
			else:
				s += card.get_name(pl) + " "
				if card.type & TYPE_LINK:
					s += (pl._("({attack}) level {level}")
						.format(attack=card.attack, level=card.level))
				else:
					s += (pl._("({attack}/{defense}) level {level}")
						.format(attack=card.attack, defense=card.defense, level=card.level))
				s += " " + card.get_position(pl)

				if len(card.xyz_materials):
					s += " ("+pl._("xyz materials: %d")%(len(card.xyz_materials))+")"
				counters = []
				for c in card.counters:
					counter_type = c & 0xffff
					counter_val = (c >> 16) & 0xffff
					counter_type = globals.strings[pl.language]['counter'][counter_type]
					counter_str = "%s: %d" % (counter_type, counter_val)
					counters.append(counter_str)
				if counters:
					s += " (" + ", ".join(counters) + ")"
			pl.notify(s)
		for card in sz:
			s = "s%d: " % (card.sequence + 1)
			if hide_facedown and card.position in (0x8, 0xa):
				s += card.get_position(pl)
			else:
				s += card.get_name(pl) + " "
				s += card.get_position(pl)

				if card.equip_target:

					s += ' ' + pl._('(equipped to %s)')%(card.equip_target.get_spec(pl.duel_player))

				counters = []
				for c in card.counters:
					counter_type = c & 0xffff
					counter_val = (c >> 16) & 0xffff
					counter_type = globals.strings[pl.language]['counter'][counter_type]
					counter_str = "%s: %d" % (counter_type, counter_val)
					counters.append(counter_str)
				if counters:
					s += " (" + ", ".join(counters) + ")"

			pl.notify(s)

		for card in mz:
			if card.type & TYPE_LINK:
				zone = self.get_linked_zone(card)
				if zone == '':
					continue
				pl.notify(pl._("Zone linked by %s (%s): %s")%(card.get_name(pl), card.get_spec(player), zone))

	def show_cards_in_location(self, pl, player, location, hide_facedown=False):
		cards = self.get_cards_in_location(player, location)
		if not cards:
			pl.notify(pl._("Table is empty."))
			return
		for card in cards:
			s = card.get_spec(player) + " "
			if hide_facedown and card.position in (0x8, 0xa):
				s += card.get_position(pl)
			else:
				s += card.get_name(pl) + " "
				s += card.get_position(pl)
				if card.type & TYPE_MONSTER:
					s += " " + pl._("level %d") % card.level
			pl.notify(s)

	def show_hand(self, pl, player):
		h = self.get_cards_in_location(player, LOCATION_HAND)
		if not h:
			pl.notify(pl._("Your hand is empty."))
			return
		for c in h:
			pl.notify("h%d: %s" % (c.sequence + 1, c.get_name(pl)))

	def show_score(self, pl):
		player = pl.duel_player
		deck = self.get_cards_in_location(player, LOCATION_DECK)
		odeck = self.get_cards_in_location(1 - player, LOCATION_DECK)
		grave = self.get_cards_in_location(player, LOCATION_GRAVE)
		ograve = self.get_cards_in_location(1 - player, LOCATION_GRAVE)
		hand = self.get_cards_in_location(player, LOCATION_HAND)
		ohand = self.get_cards_in_location(1 - player, LOCATION_HAND)
		removed = self.get_cards_in_location(player, LOCATION_REMOVED)
		oremoved = self.get_cards_in_location(1 - player, LOCATION_REMOVED)
		if pl.watching:
			nick0 = self.players[0].nickname
			nick1 = self.players[1].nickname
			pl.notify(pl._("LP: %s: %d %s: %d") % (nick0, self.lp[player], nick1, self.lp[1 - player]))
			pl.notify(pl._("Hand: %s: %d %s: %d") % (nick0, len(hand), nick1, len(ohand)))
			pl.notify(pl._("Deck: %s: %d %s: %d") % (nick0, len(deck), nick1, len(odeck)))
			pl.notify(pl._("Grave: %s: %d %s: %d") % (nick0, len(grave), nick1, len(ograve)))
			pl.notify(pl._("Removed: %s: %d %s: %d") % (nick0, len(removed), nick1, len(oremoved)))
		else:
			pl.notify(pl._("Your LP: %d Opponent LP: %d") % (self.lp[player], self.lp[1 - player]))
			pl.notify(pl._("Hand: You: %d Opponent: %d") % (len(hand), len(ohand)))
			pl.notify(pl._("Deck: You: %d Opponent: %d") % (len(deck), len(odeck)))
			pl.notify(pl._("Grave: You: %d Opponent: %d") % (len(grave), len(ograve)))
			pl.notify(pl._("Removed: You: %d Opponent: %d") % (len(removed), len(oremoved)))
		if self.paused:
			pl.notify(pl._("This duel is currently paused."))
		else:
			if not pl.watching and pl.duel_player == self.tp:
				pl.notify(pl._("It's your turn."))
			else:
				pl.notify(pl._("It's %s's turn.")%(self.players[self.tp].nickname))

	def show_info(self, card, pl):
		pln = pl.duel_player
		cs = card.get_spec(pln)
		if card.position in (0x8, 0xa) and (pl.watching or card in self.get_cards_in_location(1 - pln, LOCATION_MZONE) + self.get_cards_in_location(1 - pln, LOCATION_SZONE)):
			pl.notify(pl._("%s: %s card.") % (cs, card.get_position(pl)))
			return
		pl.notify(card.get_info(pl))

	def show_info_cmd(self, pl, spec):
		cards = []
		for i in (0, 1):
			for j in (LOCATION_MZONE, LOCATION_SZONE, LOCATION_GRAVE, LOCATION_REMOVED, LOCATION_HAND, LOCATION_EXTRA):
				cards.extend(card for card in self.get_cards_in_location(i, j) if card.controller == pl.duel_player or card.position not in (0x8, 0xa))
		specs = {}
		for card in cards:
			specs[card.get_spec(pl.duel_player)] = card
		for i, card in enumerate(pl.card_list):
			specs[str(i + 1)] = card
		if spec not in specs:
			pl.notify(pl._("Invalid card."))
			return
		self.show_info(specs[spec], pl)

	def start_debug(self, options):
		self.debug_mode = True
		lt = datetime.datetime.now().strftime('%Y-%m-%d_%H:%M:%S')
		fn = lt+"_"+self.players[0].nickname+"_"+self.players[1].nickname
		self.debug_fp = open(os.path.join('duels', fn), 'w')
		self.debug(event_type='start', player0=self.players[0].nickname, player1=self.players[1].nickname,
		deck0=self.cards[0], deck1=self.cards[1], seed=self.seed, options = options)

	def player_disconnected(self, player):
		if not self.paused:
			# all players returned successfully
			self.pause()
		else:
			# the player returned, but there are players left who need to reconnect
			player.set_parser('LobbyParser')

	def player_reconnected(self, pl):
		if not self.paused:
			self.unpause()

	def pause(self):
		for pl in self.players + self.watchers:
			pl.notify(pl._("Duel paused until all duelists reconnect."))
		for pl in self.players:
			if pl.connection is not None:
				pl.paused_parser = pl.connection.parser
				pl.set_parser('LobbyParser')

		for w in self.watchers:
			w.set_parser('LobbyParser')

	def unpause(self):
		for pl in self.players:
			pl.connection.parser = pl.paused_parser
			pl.paused_parser = None
		for w in self.watchers:
			w.set_parser('DuelParser')

		for pl in self.players+self.watchers:
			pl.notify(pl._("Duel continues."))

	def remove_watcher(self, pl):
		try:
			self.watchers.remove(pl)
			for p in self.players+self.watchers:
				if p.watch:
					p.notify(p._("%s is no longer watching this duel.")%(pl.nickname))
			pl.duel = None
			pl.watching = False
			pl.notify(pl._("Watching stopped."))
			pl.set_parser('LobbyParser')
		except ValueError:
			pass

	def add_watcher(self, pl):
		pl.duel = self
		pl.duel_player = 0
		pl.watching = True
		pl.notify(pl._("Watching duel between %s and %s.")%(self.players[0].nickname, self.players[1].nickname))
		self.watchers.append(pl)
		for p in self.players+self.watchers:
			if p.watch and p is not pl:
				p.notify(p._("%s is now watching this duel.")%(pl.nickname))
		if self.paused:
			pl.notify(pl._("The duel is currently paused due to not all players being connected."))
		else:
			pl.set_parser('DuelParser')

	def get_linked_zone(self, card):

		lst = []

		zone = lib.query_linked_zone(self.duel, card.controller, card.location, card.sequence)

		i = 0

		for i in range(8):
			if zone & (1<<i):
				lst.append('m'+str(i+1))

		for i in range(16, 24, 1):
			if zone & (1<<i):
				lst.append('om'+str(i-15))

		return ', '.join(lst)

	@property
	def paused(self):
		return len(self.players) != len([p for p in self.players if p.connection is not None])

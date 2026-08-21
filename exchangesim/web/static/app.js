/* The board.
 *
 * Two rules run through all of this.
 *
 * Prices are strings and stay strings. The simulator holds them as integers in
 * the venue's smallest increment and renders them once, at the boundary; parsing
 * them back into a JavaScript number would reintroduce exactly the binary
 * floating-point error PriceCodec exists to avoid. Comparisons here are string
 * equality between values that came from the same formatter, which is why
 * marking the best bid or the last trade needs no arithmetic at all.
 *
 * A book event is a signal, not a payload. The venue publishes only the top of
 * book on a change, so the board treats any book event as "refetch the ladder",
 * coalesced to a few times a second. Publishing full depth on every change would
 * put rendering work in the matching path for no benefit at simulator volumes.
 */
(function () {
  "use strict";

  var REFRESH_COALESCE_MS = 100;
  var TAPE_ROWS = 40;
  var AUDIT_POLL_MS = 1000;
  var AUDIT_ROWS = 400;
  /* Heartbeat is MsgType 0 in every FIX version either venue speaks; the name
     is looked up in the venue's dictionary all the same, so a dialect that
     numbered it otherwise would still be filtered. */
  var HEARTBEAT_TYPES = ["0"];
  var HEARTBEAT_NAME = "Heartbeat";

  var OWNER = "WEB";

  var el = {};
  ["venue", "market", "symbol", "instrument-name", "state", "feed", "rows",
   "ita", "tape", "error",
   "ticket", "blotter", "blotter-rows", "order-form", "order-qty",
   "order-price", "order-tif", "order-submit", "order-result", "owner-name",
   "l1-bid", "l1-bid-qty", "l1-ask", "l1-ask-qty", "l1-last", "l1-last-qty",
   "l1-spread", "l1-open", "l1-high", "l1-low", "l1-vwap", "l1-volume",
   "l1-turnover", "l1-trades", "l1-limit-up", "l1-limit-down", "l1-tick",
   "l1-lot", "auction", "auc-kind", "auc-iep", "auc-iev", "auc-imbalance",
   "auc-reference", "auc-band", "auc-reason",
   "state-control", "state-set",
   "view-toggle", "board-view", "audit-view", "audit-rows", "audit-scope",
   "audit-kind", "audit-direction", "audit-type", "audit-since", "audit-until",
   "audit-no-heartbeats", "audit-scroll",
   "audit-pause", "audit-count", "audit-detail-title", "audit-detail-fields",
   "audit-detail-raw"].forEach(function (id) {
    el[id] = document.getElementById(id);
  });

  var state = {
    venue: null,
    market: null,
    symbol: null,
    instruments: {},
    stream: null,
    pending: null,
    marketControl: false,
    // Which side of the board the buying side sits on. Every placement that
    // depends on it is CSS `order` off a <html> attribute, except the ladder,
    // whose cells are table cells and have to be built in the right sequence.
    buyRight: true,
    // The ladder's prices, highest first, exactly as the venue formatted them.
    // Stepping the price field walks this rather than doing arithmetic: tick
    // size varies with price, so only the venue can say what the next valid
    // price is, and it already did when it built these rows.
    ladderPrices: [],

    // -- the audit view --
    view: "board",
    audit: false,          // whether the server serves the audit at all
    auditTimer: null,
    auditPaused: false,
    auditAfter: 0,         // highest sequence number seen; 0 means "load afresh"
    auditSelected: null,
    auditTypes: null,      // fetched once per venue, for the filter menu
    // The MsgTypes the "no HB" filter hides, taken from the venue's own
    // dialect rather than assumed, with the FIX value as the fallback.
    auditHeartbeatTypes: HEARTBEAT_TYPES
  };

  /* The board is deep-linkable: ?venue=&market=&symbol= select on load, so a
   * board for one instrument can be bookmarked or opened from a script, and
   * ?live=0 leaves the event stream shut for a static snapshot. */
  var params = new URLSearchParams(window.location.search);

  function preferred(name, available) {
    var wanted = params.get(name);
    return (wanted && available.indexOf(wanted) >= 0) ? wanted : null;
  }

  function values(select) {
    return Array.prototype.map.call(select.options, function (o) {
      return o.value;
    });
  }

  // -- transport ----------------------------------------------------------

  function call(command, args) {
    return fetch("/api/" + encodeURIComponent(state.venue) + "/" + command, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(args || {})
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok || !body.ok) {
          var error = body.error || {};
          throw new Error(error.message || response.statusText);
        }
        return body.result;
      });
    });
  }

  // For commands whose absence is information rather than a fault: a venue
  // that has no such command, or has it but is not currently in a state where
  // it answers. Resolves to null instead of rejecting.
  function optional(command, args) {
    return call(command, args).catch(function () { return null; });
  }

  function fail(message) {
    el.error.textContent = message;
    el.error.hidden = false;
  }

  function clearError() {
    el.error.hidden = true;
  }

  // -- small helpers ------------------------------------------------------

  function text(node, value) {
    node.textContent = (value === null || value === undefined || value === "")
      ? "–" : value;
  }

  function cell(row, className, value) {
    var td = document.createElement("td");
    td.className = className;
    td.textContent = value;
    row.appendChild(td);
    return td;
  }

  function option(value, label) {
    var node = document.createElement("option");
    node.value = value;
    node.textContent = label;
    return node;
  }

  // Blank for zero. This is the ladder's formatter: an empty price level must
  // read as empty, not as a column of noughts.
  function qty(value) {
    return value ? value.toLocaleString() : "";
  }

  // Zero is a number. Anywhere a quantity is a *fact* rather than a fill level
  // -- "0 done, 0 left" on a killed order, a symbol that has not traded today
  // -- blanking it loses the answer, so only a genuinely absent value blanks.
  function num(value) {
    return (value === null || value === undefined) ? "" : value.toLocaleString();
  }

  // The venue keeps UTC and says so with a trailing Z; a board is read
  // wherever its reader is sitting, so the conversion happens here. Formatted
  // by hand rather than with toLocaleTimeString, which would give a 12-hour
  // clock in some locales and break a column that has to stay one width.
  function clockOf(iso) {
    if (!iso) { return ""; }
    var when = new Date(iso);
    if (isNaN(when.getTime())) { return iso.slice(11, 19); }
    return [when.getHours(), when.getMinutes(), when.getSeconds()]
      .map(function (part) { return String(part).padStart(2, "0"); })
      .join(":");
  }

  // -- selectors ----------------------------------------------------------

  function loadVenues() {
    return fetch("/api/venues")
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        el.venue.innerHTML = "";
        payload.venues.forEach(function (venue) {
          var node = option(venue.key,
            venue.label + (venue.connected ? "" : " (offline)"));
          el.venue.appendChild(node);
        });
        if (!payload.venues.length) {
          throw new Error("no venues are configured");
        }
        el.venue.value = preferred("venue", values(el.venue)) || el.venue.value;
        state.venue = el.venue.value;
        // The server decides whether order entry exists at all; hiding it here
        // is presentation, and the refusal is enforced there regardless.
        el.ticket.hidden = !payload.allow_order_entry;
        el.blotter.hidden = !payload.allow_order_entry;
        state.marketControl = !!payload.allow_market_control;
        el["state-control"].hidden = !state.marketControl;
        state.audit = payload.allow_audit !== false;
        el["view-toggle"].hidden = !state.audit;
        el["owner-name"].textContent = OWNER;
        applyBoardLayout(payload.board);
        return loadMarkets();
      })
      .catch(function (error) { fail("cannot list venues: " + error.message); });
  }

  /* Which side buys, and in what colour. Both come from the server's config so
   * that everyone opening this board reads it the same way round -- a per-
   * browser setting would mean two people describing the same screen
   * differently, which on a trading board is a real hazard.
   *
   * The colours are applied by pointing the --bid and --ask aliases at a hue
   * token rather than at a literal, so each keeps its own value in the light
   * theme and the contrast the palette guarantees survives the swap. */
  function applyBoardLayout(board) {
    board = board || {};
    state.buyRight = board.buy_side !== "left";
    document.documentElement.setAttribute(
      "data-buy-side", state.buyRight ? "right" : "left");

    if (board.buy_colour) {
      setHue("--bid", board.buy_colour);
    }
    if (board.sell_colour) {
      setHue("--ask", board.sell_colour);
    }

    // The ladder's header is static markup, so it is the one thing that has to
    // be reordered by hand; its rows are built to match in renderLadder.
    if (!state.buyRight) {
      var header = document.querySelector("table.ita thead tr");
      if (header) {
        // Reversed by re-appending, not by moving one cell: insertBefore of
        // the last cell in front of the first leaves the price column at the
        // end, which is where it does not belong.
        Array.prototype.slice.call(header.children).reverse()
          .forEach(function (th) { header.appendChild(th); });
      }
    }
  }

  function setHue(alias, name) {
    // A name, not a colour: the server validates it against the palette, and
    // anything else would let a config write an illegible board.
    var root = document.documentElement;
    root.style.setProperty(alias, "var(--hue-" + name + ")");
    root.style.setProperty(alias + "-bg", "var(--hue-" + name + "-wash)");
  }

  function loadMarkets() {
    return call("markets", {}).then(function (result) {
      el.market.innerHTML = "";
      result.markets.forEach(function (market) {
        el.market.appendChild(option(market.market,
          market.market + " — " + (market.description || "")));
      });
      el.market.value = preferred("market", values(el.market)) || el.market.value;
      state.market = el.market.value;

      // The venue names its own phases, so the menu is never a second copy of
      // the vocabulary that could fall out of step with the engine's.
      el["state-set"].innerHTML = "";
      el["state-set"].appendChild(option("", "set phase…"));
      (result.states || []).forEach(function (name) {
        el["state-set"].appendChild(option(name, name));
      });

      return loadInstruments();
    });
  }

  function loadInstruments() {
    return call("instruments", {}).then(function (result) {
      var previous = state.symbol;
      el.symbol.innerHTML = "";
      state.instruments = {};
      result.instruments.forEach(function (instrument) {
        state.instruments[instrument.symbol] = instrument;
        el.symbol.appendChild(option(
          instrument.symbol,
          instrument.symbol + "  " + (instrument.name || "")));
      });
      var wanted = preferred("symbol", values(el.symbol));
      if (wanted) {
        el.symbol.value = wanted;
      } else if (previous && state.instruments[previous]) {
        el.symbol.value = previous;
      }
      state.symbol = el.symbol.value;
      return select();
    });
  }

  // -- the selected instrument -------------------------------------------

  function select() {
    var instrument = state.instruments[state.symbol] || {};
    text(el["instrument-name"], instrument.name || "");
    text(el["l1-lot"], instrument.lot_size);

    // Board lots differ by security -- 100 for one, 500 for the next -- so the
    // spinner steps by this instrument's, and the default quantity is one lot
    // rather than a number carried over from whatever was selected before.
    // `min` must be the lot too, not 1: a number input counts its steps from
    // `min`, so a 500 lot anchored at 1 would offer 1, 501, 1001.
    var lot = instrument.lot_size || 1;
    el["order-qty"].step = lot;
    el["order-qty"].min = lot;
    el["order-qty"].value = lot;

    el.tape.innerHTML = "";
    openStream();
    if (state.view === "audit") {
      // The audit follows the ticker, so changing it reloads the tape.
      return reloadAudit();
    }
    return refreshAll();
  }

  function refreshAll() {
    return Promise.all([refreshBoard(), refreshTape(), refreshState(),
                        refreshBlotter()])
      .then(clearError)
      .catch(function (error) { fail(error.message); });
  }

  // While the audit view is up the board's panels are not on screen, so an
  // event burst must not do DOM work on hidden nodes. Switching back re-reads
  // everything once, so nothing is left stale.
  function boardHidden() {
    return state.view !== "board";
  }

  // A book event only says "something moved", so several arriving together
  // must collapse into one round trip rather than one each.
  function scheduleBoardRefresh() {
    if (state.pending || boardHidden()) { return; }
    state.pending = window.setTimeout(function () {
      state.pending = null;
      refreshBoard().then(clearError).catch(function (error) {
        fail(error.message);
      });
    }, REFRESH_COALESCE_MS);
  }

  function refreshBoard() {
    if (boardHidden()) { return Promise.resolve(); }
    var args = {
      symbol: state.symbol,
      market: state.market,
      rows: Math.max(1, Math.min(50, parseInt(el.rows.value, 10) || 10))
    };
    return Promise.all([
      call("ladder", args),
      call("bbo", { symbol: state.symbol, market: state.market }),
      call("stats", { symbol: state.symbol, market: state.market }),
      // Not every venue holds auctions, and one that does is not always in
      // one. Either way this command simply is not answerable, which is a
      // normal outcome and not an error worth showing.
      optional("auction", { symbol: state.symbol, market: state.market })
    ]).then(function (results) {
      renderLevel1(results[1], results[2], results[0]);
      renderLadder(results[0], results[1], results[2]);
      renderAuction(results[3]);
    });
  }

  function refreshTape() {
    if (boardHidden()) { return Promise.resolve(); }
    return call("trades", {
      symbol: state.symbol, market: state.market, limit: TAPE_ROWS
    }).then(function (result) {
      el.tape.innerHTML = "";
      result.trades.slice().reverse().forEach(function (trade) {
        el.tape.appendChild(tradeRow(trade, false));
      });
    });
  }

  function refreshState() {
    return call("state.get", {
      market: state.market, symbol: state.symbol
    }).then(function (result) { renderState(result.state); });
  }

  // -- rendering ----------------------------------------------------------

  function renderState(value) {
    var name = (value || "unknown").toLowerCase();
    var kind = "unknown";
    if (name === "open") { kind = "open"; }
    else if (name === "halted") { kind = "halted"; }
    else if (name === "closed") { kind = "closed"; }
    else if (name.indexOf("auction") >= 0 || name === "pre_open") {
      kind = "auction";
    }
    el.state.className = "badge badge-" + kind;
    el.state.textContent = value || "unknown";
  }

  function renderLevel1(bbo, stats, ladder) {
    text(el["l1-bid"], bbo.bid);
    text(el["l1-bid-qty"], qty(bbo.bid_qty));
    text(el["l1-ask"], bbo.ask);
    text(el["l1-ask-qty"], qty(bbo.ask_qty));
    text(el["l1-spread"], bbo.spread);

    text(el["l1-last"], stats.last);
    text(el["l1-last-qty"], qty(stats.last_qty));
    text(el["l1-open"], stats.open);
    text(el["l1-high"], stats.high);
    text(el["l1-low"], stats.low);
    text(el["l1-vwap"], stats.vwap);
    text(el["l1-volume"], num(stats.volume));
    text(el["l1-turnover"], stats.turnover);
    text(el["l1-trades"], stats.trades);

    text(el["l1-limit-up"], ladder.limit_up);
    text(el["l1-limit-down"], ladder.limit_down);
    text(el["l1-tick"], ladder.tick);
  }

  function renderAuction(auction) {
    if (!auction) {
      el.auction.hidden = true;
      return;
    }
    el.auction.hidden = false;
    text(el["auc-kind"], auction.auction + " — " + auction.stage);
    text(el["auc-iep"], auction.iep);
    text(el["auc-iev"], num(auction.iev));
    text(el["auc-imbalance"],
         num(auction.imbalance) +
         (auction.imbalance_side ? " " + auction.imbalance_side : ""));
    text(el["auc-reference"], auction.reference_price);
    text(el["auc-band"],
         auction.price_low && auction.price_high
           ? auction.price_low + " – " + auction.price_high : null);
    text(el["auc-reason"], auction.reason);
  }

  function renderLadder(ladder, bbo, stats) {
    el.ita.innerHTML = "";
    state.ladderPrices = ladder.rows.map(function (row) { return row.price; });

    if (!ladder.rows.length) {
      var blank = document.createElement("tr");
      blank.className = "empty-board";
      var td = document.createElement("td");
      td.colSpan = 3;
      td.textContent = "no reference price to centre on";
      blank.appendChild(td);
      el.ita.appendChild(blank);
      return;
    }

    // At-auction orders sit above OVER because they outrank every limit price:
    // they take whatever the auction settles on.
    if (ladder.ask_at_auction) {
      el.ita.appendChild(
        edgeRow("MARKET", qty(ladder.ask_at_auction), "ask-col"));
    }
    if (ladder.over) {
      el.ita.appendChild(edgeRow("OVER", qty(ladder.over), "ask-col"));
    }

    ladder.rows.forEach(function (row) {
      var tr = document.createElement("tr");
      var classes = [];
      if (row.ask_qty) { classes.push("has-ask"); }
      if (row.bid_qty) { classes.push("has-bid"); }
      // String equality, not arithmetic: both sides came from one formatter.
      if (bbo.ask && row.price === bbo.ask) { classes.push("best-ask"); }
      if (bbo.bid && row.price === bbo.bid) { classes.push("best-bid"); }
      if (stats.last && row.price === stats.last) { classes.push("last"); }
      if (row.price === ladder.limit_up || row.price === ladder.limit_down) {
        classes.push("limit");
      }
      tr.className = classes.join(" ");

      ladderCells(tr, qty(row.ask_qty), row.price, qty(row.bid_qty));
      el.ita.appendChild(tr);
    });

    if (ladder.under) {
      el.ita.appendChild(edgeRow("UNDER", qty(ladder.under), "bid-col"));
    }
    if (ladder.bid_at_auction) {
      el.ita.appendChild(
        edgeRow("MARKET", qty(ladder.bid_at_auction), "bid-col"));
    }
  }

  function edgeRow(label, value, side) {
    var tr = document.createElement("tr");
    tr.className = "edge";
    if (side === "ask-col") {
      ladderCells(tr, value, label, "");
    } else {
      ladderCells(tr, "", label, value);
    }
    return tr;
  }

  /* The one place that knows which way round the ladder runs. The cells keep
   * their own classes whichever order they are written in, so the colouring,
   * the best-price markers and the click-to-fill selector are all unaffected. */
  function ladderCells(tr, ask, price, bid) {
    if (state.buyRight) {
      cell(tr, "ask-col", ask);
      cell(tr, "price-col", price);
      cell(tr, "bid-col", bid);
    } else {
      cell(tr, "bid-col", bid);
      cell(tr, "price-col", price);
      cell(tr, "ask-col", ask);
    }
  }

  function tradeRow(trade, isNew) {
    var tr = document.createElement("tr");
    // An auction trade has no aggressor at all, so it is neither colour --
    // painting it as a sell would invent a direction the venue never reported.
    var side = trade.aggressor === "BUY" ? "buy"
             : trade.aggressor === "SELL" ? "sell" : "auction";
    tr.className = side + (isNew ? " flash" : "");
    cell(tr, "time", clockOf(trade.time));
    cell(tr, "num", trade.price);
    cell(tr, "num", qty(trade.quantity));
    cell(tr, "side", trade.aggressor || "auction");
    return tr;
  }

  // -- order entry --------------------------------------------------------

  function refreshBlotter() {
    if (el.blotter.hidden || boardHidden()) { return Promise.resolve(); }
    return call("orders", {
      owner: OWNER, market: state.market, symbol: state.symbol, live: true
    }).then(renderBlotter);
  }

  function renderBlotter(result) {
    el["blotter-rows"].innerHTML = "";
    if (!result.orders.length) {
      var none = document.createElement("tr");
      none.className = "none";
      var td = document.createElement("td");
      td.colSpan = 5;
      td.textContent = "nothing working";
      none.appendChild(td);
      el["blotter-rows"].appendChild(none);
      return;
    }
    result.orders.forEach(function (order) {
      var tr = document.createElement("tr");
      tr.className = order.side === "BUY" ? "buy" : "sell";
      cell(tr, "", order.cl_ord_id);
      cell(tr, "side", order.side);
      cell(tr, "num", order.price);
      cell(tr, "num", num(order.leaves_qty));

      var actions = document.createElement("td");
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = "cancel";
      button.addEventListener("click", function () {
        button.disabled = true;
        call("order.cancel", { order_id: order.order_id })
          .then(refreshBlotter)
          .catch(function (error) { fail(error.message); });
      });
      actions.appendChild(button);
      tr.appendChild(actions);
      el["blotter-rows"].appendChild(tr);
    });
  }

  function submitOrder(event) {
    event.preventDefault();
    var side = el["order-form"].querySelector("input[name=side]:checked").value;
    var price = el["order-price"].value.trim();
    if (!price) {
      return note("a price is required — click one on the ladder", false);
    }

    el["order-submit"].disabled = true;
    call("order.new", {
      market: state.market,
      symbol: state.symbol,
      side: side,
      quantity: parseInt(el["order-qty"].value, 10),
      price: price,
      tif: el["order-tif"].value,
      owner: OWNER
    }).then(function (result) {
      var order = result.order || {};
      // A rejected order is a successful command: the venue answered, and what
      // it answered is the interesting part.
      var rejected = order.status === "REJECTED";
      note(rejected
        ? "rejected: " + reasonOf(result)
        : order.cl_ord_id + " " + order.status.toLowerCase() +
          " (" + num(order.cum_qty) + " done, " + num(order.leaves_qty) + " left)",
        !rejected);
      return Promise.all([refreshBlotter(), refreshBoard()]);
    }).catch(function (error) {
      note(error.message, false);
    }).then(function () {
      el["order-submit"].disabled = false;
    });
  }

  function reasonOf(result) {
    var first = (result.events || [])[0] || {};
    return (first.reason || "unknown") + (first.text ? " — " + first.text : "");
  }

  function note(message, ok) {
    el["order-result"].textContent = message;
    el["order-result"].className = "result " + (ok ? "ok" : "bad");
  }

  // -- the audit view -----------------------------------------------------
  //
  // A tape rather than a table: rows are inserted and never rebuilt, so the row
  // being read keeps its selection while new traffic arrives. Newest is at the
  // top, so the entry somebody is waiting for is on screen without scrolling --
  // which is also why a poll that inserts above the viewport pushes the scroll
  // position down by as much, leaving what is being read where it was. The tail
  // is an incremental poll -- each request asks only for what is newer than the
  // highest sequence number already shown -- which is why an audit entry needs
  // no event plumbing of its own.

  function setView(name) {
    state.view = name;
    el["board-view"].hidden = name !== "board";
    el["audit-view"].hidden = name !== "audit";
    el["view-toggle"].textContent = name === "board" ? "audit" : "board";

    if (name === "audit") {
      loadAuditTypes()
        .then(reloadAudit)
        .catch(function (error) { fail(error.message); });
    } else {
      stopAuditPoll();
      refreshAll();
    }
  }

  function loadAuditTypes() {
    if (state.auditTypes) { return Promise.resolve(); }
    return call("audit.types", {}).then(function (result) {
      state.auditTypes = result;
      var heartbeats = (result.msg_types || []).filter(function (entry) {
        return entry.name === HEARTBEAT_NAME;
      }).map(function (entry) { return entry.type; });
      state.auditHeartbeatTypes = heartbeats.length ? heartbeats
                                                    : HEARTBEAT_TYPES;
      el["audit-type"].innerHTML = "";
      el["audit-type"].appendChild(option("", "any message"));
      // Straight from the venue's own dialect, so the menu cannot drift from
      // what this venue can actually send or receive.
      (result.msg_types || []).forEach(function (entry) {
        el["audit-type"].appendChild(
          option(entry.type, entry.name + "  (" + entry.type + ")"));
      });
      (result.commands || []).forEach(function (name) {
        el["audit-type"].appendChild(option(name, name));
      });
    });
  }

  function auditArgs(tail) {
    var args = { limit: AUDIT_ROWS };
    if (el["audit-scope"].value === "symbol") { args.symbol = state.symbol; }
    if (el["audit-kind"].value) { args.kind = el["audit-kind"].value; }
    if (el["audit-direction"].value) {
      args.direction = el["audit-direction"].value;
    }
    if (el["audit-type"].value) { args.types = [el["audit-type"].value]; }
    // Excluded server-side, so a quiet session's heartbeats do not fill the
    // page's worth of entries the venue was asked for.
    if (el["audit-no-heartbeats"].checked) {
      args.exclude_types = state.auditHeartbeatTypes;
    }
    if (el["audit-since"].value.trim()) {
      args.since = el["audit-since"].value.trim();
    }
    if (el["audit-until"].value.trim()) {
      args.until = el["audit-until"].value.trim();
    }
    if (tail) { args.after = state.auditAfter; }
    return args;
  }

  function reloadAudit() {
    state.auditAfter = 0;
    el["audit-rows"].innerHTML = "";
    return pollAudit().then(scheduleAuditPoll);
  }

  function pollAudit() {
    if (!state.audit || state.view !== "audit") { return Promise.resolve(); }
    var tail = state.auditAfter > 0;
    return call("audit", auditArgs(tail)).then(function (result) {
      var scroller = el["audit-scroll"];
      var before = scroller.scrollHeight;
      // The gap marker goes in first so the entries of this poll end up above
      // it: it stands for traffic older than they are.
      if (result.truncated && tail) { prependGapRow(); }
      // The venue answers oldest first, so inserting each at the top leaves the
      // batch newest first, which is the order the whole list is in.
      result.entries.forEach(function (entry) {
        prependRow(auditRow(entry));
      });
      // Measured before the trim, which takes rows off the *bottom*: those are
      // below whatever is being read and move nothing, so counting them here
      // would leave the compensation short.
      var added = scroller.scrollHeight - before;
      // Advance past entries a filter excluded as well as those shown, or every
      // poll would rescan the same traffic.
      state.auditAfter = result.last_seq;
      trimAudit();
      // Rows arriving above the viewport would otherwise carry the row being
      // read downwards. At the top there is nothing to preserve, so the new
      // traffic simply appears.
      if (scroller.scrollTop > 0) { scroller.scrollTop += added; }
      text(el["audit-count"],
           el["audit-rows"].childNodes.length + " shown, " +
           result.capacity + " kept" +
           (state.auditPaused ? " — paused" : ""));
      clearError();
    }).catch(function (error) { fail(error.message); });
  }

  function prependRow(tr) {
    var rows = el["audit-rows"];
    rows.insertBefore(tr, rows.firstChild);
  }

  function trimAudit() {
    // The oldest row is now the last one, so that is the end trimming takes
    // from -- dropping the first would throw away the newest traffic.
    var rows = el["audit-rows"];
    while (rows.childNodes.length > AUDIT_ROWS) {
      rows.removeChild(rows.lastChild);
    }
  }

  function prependGapRow() {
    var tr = document.createElement("tr");
    tr.className = "gap";
    var td = document.createElement("td");
    td.colSpan = 5;
    td.textContent = "… entries were overwritten before they could be shown";
    tr.appendChild(td);
    prependRow(tr);
  }

  function auditRow(entry) {
    var tr = document.createElement("tr");
    // "kind-" prefixed on purpose: a bare `control` class would collide with
    // the market-control widget's `.control { display: flex }` in the header,
    // and a table row laid out as a flex container is a strange sight.
    tr.className = "kind-" + entry.kind +
      (entry.direction === "in" ? " inbound" : " outbound") +
      (entry.symbol ? "" : " session-level") +
      (entry.ok === false ? " failed" : "");
    tr.dataset.seq = entry.seq;

    cell(tr, "time", clockOf(entry.time));
    // An arrow rather than a word: direction is what the eye runs down, and it
    // should read as a shape. Pointing the same way the session log does --
    // "<--" for what arrived, "-->" for what the venue sent.
    cell(tr, "dir", entry.direction === "in" ? "←" : "→");
    cell(tr, "type", entry.type_name || entry.type || "");
    cell(tr, "sym", entry.symbol || "");
    cell(tr, "summary", entry.summary || entry.error || "");

    tr.addEventListener("click", function () { selectEntry(entry.seq, tr); });
    if (entry.seq === state.auditSelected) { tr.classList.add("selected"); }
    return tr;
  }

  function selectEntry(seq, row) {
    state.auditSelected = seq;
    Array.prototype.forEach.call(
      el["audit-rows"].querySelectorAll("tr.selected"),
      function (other) { other.classList.remove("selected"); });
    if (row) { row.classList.add("selected"); }

    call("audit.entry", { seq: seq })
      .then(renderAuditDetail)
      .then(clearError)
      .catch(function (error) { fail(error.message); });
  }

  function renderAuditDetail(result) {
    var entry = result.entry;
    text(el["audit-detail-title"],
         "#" + entry.seq + "  " + (entry.type_name || entry.type || "") +
         (entry.session ? "  " + entry.session : ""));

    var body = el["audit-detail-fields"];
    body.innerHTML = "";
    if (entry.kind === "control") {
      // A command has arguments and a reply rather than tags, so it is shown as
      // what it is instead of being forced into a field table.
      el["audit-detail-raw"].textContent =
        JSON.stringify(result.detail, null, 2);
      return;
    }

    result.fields.forEach(function (field) {
      var tr = document.createElement("tr");
      if (field.required) { tr.className = "required"; }
      cell(tr, "num", field.tag);
      cell(tr, "name", field.name || "unknown");
      cell(tr, "value", field.value);
      cell(tr, "meaning", field.label || "");
      body.appendChild(tr);
    });
    el["audit-detail-raw"].textContent = result.wire || "";
  }

  function scheduleAuditPoll() {
    if (state.auditTimer || state.auditPaused ||
        state.view !== "audit" || document.hidden) {
      return;
    }
    // A self-rescheduling timeout, not an interval: an interval stacks requests
    // when one is slow, and a background tab would keep polling every venue.
    state.auditTimer = window.setTimeout(function () {
      state.auditTimer = null;
      pollAudit().then(scheduleAuditPoll, scheduleAuditPoll);
    }, AUDIT_POLL_MS);
  }

  function stopAuditPoll() {
    if (state.auditTimer) {
      window.clearTimeout(state.auditTimer);
      state.auditTimer = null;
    }
  }

  // -- live updates -------------------------------------------------------

  function openStream() {
    if (params.get("live") === "0") { return; }
    if (state.stream) { state.stream.close(); }

    var url = "/api/" + encodeURIComponent(state.venue) +
      "/events?topics=" + encodeURIComponent("book:*,trade:*,state:*");
    var stream = new EventSource(url);
    state.stream = stream;

    stream.onopen = function () {
      el.feed.className = "feed feed-live";
      el.feed.textContent = "live";
    };
    stream.onerror = function () {
      el.feed.className = "feed feed-down";
      el.feed.textContent = "reconnecting";
    };
    stream.onmessage = function (event) {
      var payload = JSON.parse(event.data);
      onTopic(payload.topic, payload.data);
    };
  }

  function onTopic(topic, data) {
    var parts = topic.split(":");
    var kind = parts[0];

    if (kind === "state") {
      if (parts[1] === state.market) { refreshState(); }
      return;
    }
    if (parts[1] !== state.market || parts[2] !== state.symbol) { return; }

    if (kind === "book") {
      scheduleBoardRefresh();
      refreshBlotter();
    } else if (kind === "trade") {
      el.tape.insertBefore(tradeRow(data, true), el.tape.firstChild);
      while (el.tape.childNodes.length > TAPE_ROWS) {
        el.tape.removeChild(el.tape.lastChild);
      }
      scheduleBoardRefresh();
    }
  }

  // -- wiring -------------------------------------------------------------

  el.venue.addEventListener("change", function () {
    state.venue = el.venue.value;
    // The filter menu is built from the venue's dialect, so it belongs to the
    // venue and must be refetched rather than carried across.
    state.auditTypes = null;
    state.auditSelected = null;
    loadMarkets().catch(function (error) { fail(error.message); });
  });

  el["view-toggle"].addEventListener("click", function () {
    setView(state.view === "board" ? "audit" : "board");
  });

  ["audit-scope", "audit-kind", "audit-direction", "audit-type",
   "audit-since", "audit-until", "audit-no-heartbeats"].forEach(function (id) {
    el[id].addEventListener("change", function () {
      reloadAudit().catch(function (error) { fail(error.message); });
    });
  });

  el["audit-pause"].addEventListener("click", function () {
    state.auditPaused = !state.auditPaused;
    el["audit-pause"].textContent = state.auditPaused ? "resume" : "pause";
    el["audit-pause"].classList.toggle("paused", state.auditPaused);
    // The cursor is untouched while paused, so resuming catches up rather than
    // skipping; the server says so if the backlog was overwritten meanwhile.
    scheduleAuditPoll();
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stopAuditPoll(); } else { scheduleAuditPoll(); }
  });

  el.market.addEventListener("change", function () {
    state.market = el.market.value;
    select();
  });

  el.symbol.addEventListener("change", function () {
    state.symbol = el.symbol.value;
    select();
  });

  el.rows.addEventListener("change", function () {
    refreshBoard().catch(function (error) { fail(error.message); });
  });

  // Clicking a ladder price fills the ticket. Nothing is parsed: the string the
  // venue formatted is the string sent back to it.
  el.ita.addEventListener("click", function (event) {
    var target = event.target.closest("td.price-col");
    if (!target) { return; }
    // The OVER and UNDER rows carry a label here, not a price.
    var price = target.textContent.trim();
    if (/^[0-9]+(\.[0-9]+)?$/.test(price)) {
      el["order-price"].value = price;
    }
  });

  // Moving a market is per market, never per symbol: leaving the symbol out is
  // what makes this the market's phase rather than an instrument-level
  // override, which is a different and much less obvious thing to do from a
  // board showing one instrument.
  el["state-set"].addEventListener("change", function () {
    var wanted = el["state-set"].value;
    el["state-set"].value = "";
    if (!wanted) { return; }
    call("state.set", { market: state.market, state: wanted })
      .then(function () { return Promise.all([refreshState(), refreshAll()]); })
      .then(clearError)
      .catch(function (error) { fail(error.message); });
  });

  el["order-form"].addEventListener("submit", submitOrder);

  // Up and down move the price one ladder row, which keeps it on a valid tick
  // even where the tick changes -- a fixed step would misalign at the first
  // threshold and give no sign it had.
  el["order-price"].addEventListener("keydown", function (event) {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") { return; }
    event.preventDefault();
    var next = stepPrice(el["order-price"].value.trim(),
                         event.key === "ArrowUp" ? 1 : -1);
    if (next !== null) { el["order-price"].value = next; }
  });

  function stepPrice(current, direction) {
    var prices = state.ladderPrices;
    if (!prices.length) { return null; }
    // Rows run highest first, so a step up is a step back through the array.
    var index = prices.indexOf(current);
    if (index < 0) {
      // Empty, or a price off the board: enter at the middle row, which is the
      // one the ladder is centred on and so the one nearest the market.
      return prices[Math.floor(prices.length / 2)];
    }
    var wanted = index - direction;
    return (wanted < 0 || wanted >= prices.length) ? current : prices[wanted];
  }

  Array.prototype.forEach.call(
    el["order-form"].querySelectorAll("input[name=side]"),
    function (radio) {
      radio.addEventListener("change", markSide);
    });

  function markSide() {
    Array.prototype.forEach.call(
      el["order-form"].querySelectorAll(".side"),
      function (label) {
        var radio = label.querySelector("input");
        label.classList.toggle("selected", radio.checked);
      });
  }

  markSide();
  loadVenues();
})();

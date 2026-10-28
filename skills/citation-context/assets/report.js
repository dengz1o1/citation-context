(function () {
  var DATA = JSON.parse(document.getElementById("cg-data").textContent);
  var NS = "http://www.w3.org/2000/svg";
  var svg = document.getElementById("graph");
  var list = document.getElementById("list");
  var detail = document.getElementById("detail");

  var byId = {};
  DATA.nodes.forEach(function (n) { byId[n.nid] = n; });

  var adj = {};
  DATA.edges.forEach(function (e) {
    (adj[e.from] = adj[e.from] || {})[e.to] = e.type;
    (adj[e.to] = adj[e.to] || {})[e.from] = e.type;
  });

  var maxCites = Math.max.apply(null, DATA.nodes.map(function (n) { return n.cites || 0; }).concat([1]));

  var root = document.createElementNS(NS, "g");
  svg.appendChild(root);
  var gEdges = document.createElementNS(NS, "g");
  var gNodes = document.createElementNS(NS, "g");
  root.appendChild(gEdges);
  root.appendChild(gNodes);

  var mode = "net";
  var selected = 0;
  var pinned = false;
  var k = 1, tx = 0, ty = 0;

  function coords(n) { return mode === "net" ? [n.x, n.y] : [n.x2, n.y2]; }

  function applyTransform() {
    root.setAttribute("transform", "translate(" + tx + "," + ty + ") scale(" + k + ")");
    updateLabels();
  }

  // ---- 颜色：相关度 → 绿阶
  var STOPS = [[233, 243, 236], [146, 198, 168], [46, 125, 91], [13, 61, 43]];
  function relColor(v) {
    v = Math.max(0, Math.min(1, v || 0));
    var pos = v * (STOPS.length - 1);
    var i = Math.min(STOPS.length - 2, Math.floor(pos));
    var f = pos - i;
    var a = STOPS[i], b = STOPS[i + 1];
    return "rgb(" + Math.round(a[0] + (b[0] - a[0]) * f) + "," +
      Math.round(a[1] + (b[1] - a[1]) * f) + "," +
      Math.round(a[2] + (b[2] - a[2]) * f) + ")";
  }

  function radius(n) { return n.nid === 0 ? 9 : 4 + 9 * Math.log1p(n.cites || 0) / Math.log1p(maxCites); }

  function labelText(n) {
    if (n.nid === 0) return "本文";
    var first = (n.authors || [])[0] || "";
    var surname = first.split(/\s+/).pop();
    return (surname ? surname + " " : "") + (n.year || "");
  }

  // ---- 画边
  var rankOf = {};
  DATA.nodes.filter(function (n) { return n.nid !== 0; })
    .sort(function (a, b) { return (b.score || 0) - (a.score || 0); })
    .forEach(function (n, i) { rankOf[n.nid] = i; });

  var edgeEls = [];
  DATA.edges.forEach(function (e) {
    var a = byId[e.from], b = byId[e.to];
    if (!a || !b) return;
    var ln = document.createElementNS(NS, "line");
    var spoke = (e.from === 0 || e.to === 0);
    var other = spoke ? (e.from === 0 ? e.to : e.from) : null;
    var faint = spoke && rankOf[other] !== undefined && rankOf[other] >= 12;
    var cls = "edge" + (e.type === "cites" ? "" : " coupled");
    if (spoke) cls += faint ? " spoke faint" : " spoke";
    ln.setAttribute("class", cls);
    gEdges.appendChild(ln);
    edgeEls.push({ el: ln, a: e.from, b: e.to, spoke: spoke });
  });

  // ---- 画点
  var nodeEls = {};
  DATA.nodes.forEach(function (n) {
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node role-" + n.role + (n.nid === 0 ? " seed-node" : ""));
    var c = document.createElementNS(NS, "circle");
    c.setAttribute("r", radius(n));
    c.setAttribute("fill", n.nid === 0 ? "#0b3d2b" : relColor(n.rel));
    g.appendChild(c);
    var t = document.createElementNS(NS, "text");
    t.textContent = labelText(n);
    t.style.display = "none";
    g.appendChild(t);
    g.addEventListener("click", function (ev) { ev.stopPropagation(); select(n.nid); });
    g.addEventListener("mouseenter", function () { hover(n.nid); });
    g.addEventListener("mouseleave", function () { hover(null); });
    gNodes.appendChild(g);
    nodeEls[n.nid] = { g: g, circle: c, text: t, r: radius(n) };
  });

  // 没有施引文献时上半区会空着，补一行说明
  if (!DATA.nodes.some(function (n) { return n.role === "forward"; })) {
    var note = document.createElementNS(NS, "text");
    note.setAttribute("x", 500);
    note.setAttribute("y", 150);
    note.setAttribute("text-anchor", "middle");
    note.setAttribute("class", "band-note");
    note.textContent = "尚无施引文献";
    gNodes.appendChild(note);
  }

  // ---- 标签避让：按得分优先级贪心放置，互相重叠的直接不显示
  function updateLabels() {
    var rest = DATA.nodes.filter(function (n) { return n.nid !== 0; })
      .sort(function (a, b) { return (b.score || 0) - (a.score || 0); });
    var want = [0].concat(rest.slice(0, k > 1.15 ? rest.length : 7).map(function (n) { return n.nid; }));
    var placed = [];
    DATA.nodes.forEach(function (n) {
      var el = nodeEls[n.nid];
      el.text.style.display = "none";
    });
    want.forEach(function (nid) {
      var n = byId[nid];
      if (!n || nodeEls[nid].g.style.display === "none") return;
      var p = coords(n);
      var r = radius(n);
      var txt = labelText(n);
      var w = txt.length * 5.8 + 6;
      var x = p[0] + r + 4, y = p[1] - 7;
      if (x + w > 975) x = p[0] - r - 4 - w;
      if (x < 5) return;
      var box = { x: x, y: y, w: w, h: 14 };
      for (var i = 0; i < placed.length; i++) {
        var b = placed[i];
        if (x < b.x + b.w && x + w > b.x && y < b.y + b.h && y + 14 > b.y) return;
      }
      placed.push(box);
      var t = nodeEls[nid].text;
      t.setAttribute("x", x);
      t.setAttribute("y", y + 11);
      t.style.display = "block";
    });
  }

  function layoutNodes() {
    DATA.nodes.forEach(function (n) {
      var p = coords(n);
      var el = nodeEls[n.nid];
      el.circle.setAttribute("cx", p[0]);
      el.circle.setAttribute("cy", p[1]);
    });
    redrawEdges();
    updateLabels();
  }

  function redrawEdges() {
    edgeEls.forEach(function (e) {
      var a = coords(byId[e.a]), b = coords(byId[e.b]);
      e.el.setAttribute("x1", a[0]); e.el.setAttribute("y1", a[1]);
      e.el.setAttribute("x2", b[0]); e.el.setAttribute("y2", b[1]);
    });
  }

  // ---- 左侧清单
  var rows = {};
  function buildList() {
    var head = document.createElement("div");
    head.className = "list-head";
    head.textContent = "按相关度排序 · " + DATA.nodes.length + " 篇";
    list.appendChild(head);

    var ordered = DATA.nodes.slice().sort(function (a, b) {
      if (a.nid === 0) return -1;
      if (b.nid === 0) return 1;
      return (b.score || 0) - (a.score || 0);
    });

    ordered.forEach(function (n, i) {
      var row = document.createElement("button");
      row.className = "row" + (n.nid === 0 ? " seed" : "");
      row.setAttribute("data-nid", n.nid);
      row.setAttribute("data-role", n.role);
      var meta = [];
      if (n.authors && n.authors.length) {
        var sur = (n.authors[0] || "").split(/\s+/).pop();
        if (sur) meta.push(sur);
      }
      if (n.venue) meta.push(n.venue);
      if (n.year) meta.push(n.year);
      if (n.role === "seed") meta.push("本文");
      else meta.push("被引 " + (n.cites || 0));

      row.innerHTML =
        '<span class="rank">' + (n.nid === 0 ? "—" : i) + "</span>" +
        '<span class="row-main"><span class="row-title">' + esc(n.title || "(无标题)") + "</span>" +
        '<span class="row-meta">' + esc(meta.join(" · ")) + "</span></span>" +
        '<span class="row-score">' + (n.nid === 0 ? "" : (n.score || 0).toFixed(2)) + "</span>";
      row.addEventListener("click", function () { select(n.nid); });
      list.appendChild(row);
      rows[n.nid] = row;
    });
  }

  function syncList(nid) {
    Object.keys(rows).forEach(function (id) {
      rows[id].classList.toggle("on", Number(id) === nid);
    });
    if (rows[nid] && rows[nid].scrollIntoView) {
      rows[nid].scrollIntoView({ block: "nearest" });
    }
  }

  // ---- 选中 / 悬停
  function clearHL() {
    DATA.nodes.forEach(function (n) { nodeEls[n.nid].g.classList.remove("dim", "sel"); });
    edgeEls.forEach(function (e) { e.el.classList.remove("dim", "hot"); });
    detail.querySelectorAll(".cite.on").forEach(function (a) { a.classList.remove("on"); });
  }

  function highlight(nid) {
    if (nid === null || nid === undefined) {
      clearHL();
      if (selected !== null && selected !== undefined) highlight(selected);
      return;
    }
    if (nid === 0) { clearHL(); return; }  // 选中 seed = 总览
    var keep = adj[nid] || {};
    DATA.nodes.forEach(function (n) {
      var on = n.nid === nid || keep[n.nid] !== undefined;
      nodeEls[n.nid].g.classList.toggle("dim", !on);
    });
    edgeEls.forEach(function (e) {
      var on = e.a === nid || e.b === nid;
      e.el.classList.toggle("dim", !on);
      e.el.classList.toggle("hot", on);
    });
  }

  function select(nid) {
    selected = nid;
    clearHL();
    nodeEls[nid].g.classList.add("sel");
    highlight(nid);
    syncList(nid);
    if (!pinned) renderDetail(nid);
  }

  function hover(nid) { highlight(nid); }

  // ---- 详情面板
  var ROLE_CN = { seed: "本文", backward: "后向引用", forward: "施引文献", sibling: "平行工作" };

  function bar(label, v) {
    var pct = Math.round(Math.max(0, Math.min(1, v || 0)) * 100);
    return '<div class="bar-row"><span>' + label + "</span>" +
      '<span class="track"><span class="fill" style="width:' + pct + '%"></span></span>' +
      "<span>" + pct + "</span></div>";
  }

  function narrativeHTML() {
    return DATA.sections.map(function (s) {
      return "<h2>" + s.title + "</h2>" +
        s.paras.map(function (p) { return "<p>" + p + "</p>"; }).join("");
    }).join("");
  }

  function renderDetail(nid) {
    var n = byId[nid];
    if (!n) { detail.innerHTML = '<p class="empty">未找到节点</p>'; return; }
    var html = '<div class="detail-head"><span class="kicker">' + (ROLE_CN[n.role] || n.role) +
      '</span><button id="pin" class="pin' + (pinned ? " on" : "") + '">' +
      (pinned ? "已固定" : "固定") + "</button></div>";
    html += "<h1>" + esc(n.title || "(无标题)") + "</h1>";
    var au = (n.authors || []).join(", ");
    if (n.author_count && n.author_count > (n.authors || []).length) {
      au += " 等 " + n.author_count + " 人";
    }
    html += '<div class="meta">' + esc(au) + "</div>";
    html += '<div class="meta">' + esc(n.venue || "—") + " · " + (n.year || "—") +
      " · 被引 " + (n.cites || 0) + "</div>";
    html += '<div class="chips"><span class="chip role-' + n.role + '">' + (ROLE_CN[n.role] || n.role) + "</span>" +
      (n.role === "seed" ? "" : '<span class="chip">' + esc(n.label || "") + "</span>") + "</div>";
    if (n.role === "seed") {
      html += '<div class="why">本文即种子文献：分项得分是相对它自己定义的，不再展示比较条。</div>';
    } else {
      html += '<div class="bars">' + bar("相关度", n.rel) + bar("文本", n.text) +
        bar("共被引", n.cocite) + bar("综合", n.score) + "</div>";
    }
    if (n.why) html += '<div class="why">' + esc(n.why) + "</div>";
    if (n.contexts && n.contexts.length) {
      html += '<div class="ctx">' + n.contexts.map(function (c) { return "&ldquo;" + esc(c) + "&rdquo;"; }).join("<br><br>") + "</div>";
    }
    if (n.abstract) html += '<div class="abstract">' + esc(n.abstract) + "</div>";
    else html += '<div class="empty">该文献在 OpenAlex / Semantic Scholar / Crossref 均未收录摘要，可点下方 DOI 查看原文。</div>';
    if (n.doi) html += '<a class="doi" href="https://doi.org/' + esc(n.doi) + '" target="_blank" rel="noopener">doi:' + esc(n.doi) + " ↗</a>";
    if (n.role === "seed" && DATA.sections.length) {
      html += '<div class="narrative">' + narrativeHTML() + "</div>";
    }
    detail.innerHTML = html;
    var pin = document.getElementById("pin");
    if (pin) pin.addEventListener("click", togglePin);
    bindCites(detail);
  }

  function togglePin() {
    pinned = !pinned;
    renderDetail(selected);
  }

  function bindCites(scope) {
    scope.querySelectorAll(".cite").forEach(function (a) {
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        var nid = Number(a.getAttribute("data-nid"));
        select(nid);
        centerOn(nid);
      });
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function centerOn(nid) {
    var p = coords(byId[nid]);
    k = Math.max(k, 1.2);
    tx = 500 - p[0] * k;
    ty = 500 - p[1] * k;
    applyTransform();
  }

  // ---- 缩放与平移（只在空白/边上起拖，避免吞掉节点点击）
  svg.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var rect = svg.getBoundingClientRect();
    var mx = (ev.clientX - rect.left) / rect.width * 1000;
    var my = (ev.clientY - rect.top) / rect.height * 1000;
    var factor = ev.deltaY < 0 ? 1.12 : 0.89;
    var nk = Math.max(0.55, Math.min(4, k * factor));
    tx = mx - (mx - tx) * (nk / k);
    ty = my - (my - ty) * (nk / k);
    k = nk;
    applyTransform();
  }, { passive: false });

  var drag = null;
  svg.addEventListener("pointerdown", function (ev) {
    if (ev.target.closest && ev.target.closest("g.node")) return;
    drag = { x: ev.clientX, y: ev.clientY, tx: tx, ty: ty };
    svg.classList.add("dragging");
  });
  window.addEventListener("pointermove", function (ev) {
    if (!drag) return;
    var rect = svg.getBoundingClientRect();
    tx = drag.tx + (ev.clientX - drag.x) / rect.width * 1000;
    ty = drag.ty + (ev.clientY - drag.y) / rect.height * 1000;
    applyTransform();
  });
  window.addEventListener("pointerup", function () { drag = null; svg.classList.remove("dragging"); });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { k = 1; tx = 0; ty = 0; applyTransform(); clearHL(); highlight(selected); }
  });

  // ---- 视图切换
  var btnNet = document.getElementById("mode-net");
  var btnYear = document.getElementById("mode-year");
  function setMode(m) {
    mode = m;
    btnNet.classList.toggle("on", m === "net");
    btnYear.classList.toggle("on", m === "year");
    svg.classList.toggle("mode-year", m === "year");
    layoutNodes();
  }
  btnNet.addEventListener("click", function () { setMode("net"); });
  btnYear.addEventListener("click", function () { setMode("year"); });

  // ---- 筛选与搜索
  function applyFilters() {
    var roles = {};
    document.querySelectorAll("input[data-role]").forEach(function (cb) { roles[cb.getAttribute("data-role")] = cb.checked; });
    var showSpokes = document.getElementById("spokes").checked;
    DATA.nodes.forEach(function (n) {
      var on = roles[n.role] !== false;
      nodeEls[n.nid].g.style.display = on ? "" : "none";
      if (rows[n.nid]) rows[n.nid].style.display = on ? "" : "none";
      n._off = !on;
    });
    edgeEls.forEach(function (e) {
      var a = byId[e.a], b = byId[e.b];
      var vis = !a._off && !b._off && !(e.spoke && !showSpokes);
      e.el.style.display = vis ? "" : "none";
    });
    updateLabels();
  }

  document.querySelectorAll("input[data-role], #spokes").forEach(function (cb) {
    cb.addEventListener("change", applyFilters);
  });

  var search = document.getElementById("search");
  search.addEventListener("input", function () {
    var q = search.value.trim().toLowerCase();
    DATA.nodes.forEach(function (n) {
      var hit = !q || (n.title || "").toLowerCase().indexOf(q) >= 0;
      if (rows[n.nid]) rows[n.nid].style.display = hit ? "" : "none";
      if (q) nodeEls[n.nid].g.classList.toggle("match", hit && (n.title || "").toLowerCase().indexOf(q) >= 0);
      else nodeEls[n.nid].g.classList.remove("match");
    });
  });

  document.getElementById("reset").addEventListener("click", function () {
    k = 1; tx = 0; ty = 0; applyTransform();
    select(0);
    search.value = "";
    DATA.nodes.forEach(function (n) { nodeEls[n.nid].g.classList.remove("match"); });
  });

  // ---- 初始化
  buildList();
  layoutNodes();
  applyTransform();
  select(0);
})();

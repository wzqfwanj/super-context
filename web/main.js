/* ============================================================
   超级上下文 · 页面脚本
   ------------------------------------------------------------
   - 拉取 api/state，渲染统计、智能体链接与资料列表
   - 多文件上传：XHR 逐个上传，显示排队 / 进度 / 结果
   - 有 processing 条目时每 3 秒轮询，页面隐藏时暂停
   - 复制链接：navigator.clipboard，失败降级 execCommand
   - 所有用户内容用 textContent / setAttribute 写入，不拼 HTML
   ============================================================ */
(function () {
  "use strict";

  /* ---------- DOM ---------- */

  function byId(id) {
    return document.getElementById(id);
  }

  var statsEl = byId("stats");
  var copyTopBtn = byId("copy-agent-top");
  var refreshBtn = byId("refresh");
  var dropzone = byId("dropzone");
  var fileInput = byId("file-input");
  var uploadsEl = byId("uploads");
  var agentLinkEl = byId("agent-link");
  var copyAgentBtn = byId("copy-agent");
  var stateArea = byId("state-area");
  var itemListEl = byId("item-list");
  var feedbackEl = byId("feedback");

  var KIND_LABEL = {
    image: "图片",
    document: "文档",
    audio: "音频",
    video: "视频",
    other: "其他"
  };

  var POLL_MS = 3000;
  var FEEDBACK_MS = 4000;

  /* ---------- 状态 ---------- */

  var state = {
    token: "",
    items: [],
    updatedAt: "",
    loaded: false
  };

  var itemNodes = {};   // id -> 条目节点引用
  var uploads = [];     // 上传队列
  var activeUpload = null;
  var inFlight = false;
  var pendingReload = false;
  var pollTimer = null;
  var feedbackTimer = null;

  /* ---------- 小工具 ---------- */

  function makeEl(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  function pad2(value) {
    return value < 10 ? "0" + value : String(value);
  }

  function formatTime(value) {
    if (!value || typeof value !== "string") return "";
    var date = new Date(value);
    if (isNaN(date.getTime())) return value.replace("T", " ").slice(0, 16);
    return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) +
      " " + pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  }

  function timeValue(value) {
    var time = Date.parse(value);
    return isNaN(time) ? 0 : time;
  }

  function formatSize(size) {
    if (typeof size !== "number" || !isFinite(size) || size < 0) return "—";
    if (size < 1024) return size + " B";
    var kb = size / 1024;
    if (kb < 1024) return (kb < 10 ? kb.toFixed(1) : String(Math.round(kb))) + " KB";
    var mb = kb / 1024;
    if (mb < 1024) return mb.toFixed(1) + " MB";
    return (mb / 1024).toFixed(1) + " GB";
  }

  function normalizeKind(kind) {
    return Object.prototype.hasOwnProperty.call(KIND_LABEL, kind) ? kind : "other";
  }

  function friendlyError(err) {
    if (!err) return "未知错误";
    if (err.name === "TypeError") return "无法连接服务（网络错误或服务未启动）";
    var message = err.message || String(err);
    return message || "未知错误";
  }

  function parseJson(text) {
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (err) {
      return null;
    }
  }

  /* ---------- 链接 ---------- */

  /* 页面挂在 /context/ 下：location.pathname 形如 /context/。
     同时兼容直接访问 /context/index.html 的情况。 */
  function sharePrefix() {
    var path = location.pathname.replace(/index\.html?$/i, "");
    path = path.replace(/\/$/, "");
    return location.origin + path;
  }

  function indexLink() {
    return state.token ? sharePrefix() + "/s/" + state.token + "/index.md" : "";
  }

  function itemLink(id) {
    return state.token ? sharePrefix() + "/s/" + state.token + "/items/" + encodeURIComponent(id) + ".md" : "";
  }

  function rawLink(id) {
    return "api/items/" + encodeURIComponent(id) + "/raw";
  }

  /* ---------- 复制（clipboard 失败降级 execCommand） ---------- */

  function copyText(text) {
    if (!text) return Promise.reject(new Error("没有可复制的链接"));
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      return navigator.clipboard.writeText(text).catch(function () {
        return legacyCopy(text);
      });
    }
    return legacyCopy(text);
  }

  function legacyCopy(text) {
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.top = "-1000px";
      area.style.opacity = "0";
      document.body.appendChild(area);
      var ok = false;
      try {
        area.select();
        area.setSelectionRange(0, area.value.length);
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      }
      document.body.removeChild(area);
      if (ok) resolve();
      else reject(new Error("复制失败"));
    });
  }

  function copyAndReport(text, successText) {
    copyText(text).then(function () {
      setFeedback(successText, false);
    }).catch(function () {
      setFeedback("复制失败，请手动选中链接复制", true);
    });
  }

  /* ---------- 反馈提示 ---------- */

  function setFeedback(text, isError) {
    if (feedbackTimer) {
      clearTimeout(feedbackTimer);
      feedbackTimer = null;
    }
    feedbackEl.textContent = text || "";
    feedbackEl.className = isError ? "feedback is-error" : "feedback";
    if (text) {
      feedbackTimer = setTimeout(function () {
        feedbackEl.textContent = "";
        feedbackEl.className = "feedback";
        feedbackTimer = null;
      }, FEEDBACK_MS);
    }
  }

  /* ---------- HTTP ---------- */

  function fetchJson(url, method) {
    var options = { method: method || "GET", cache: "no-store" };
    return fetch(url, options).then(function (response) {
      return response.text().then(function (text) {
        var data = parseJson(text);
        if (!response.ok) {
          var message = data && typeof data.error === "string"
            ? data.error
            : "请求失败（HTTP " + response.status + "）";
          throw new Error(message);
        }
        if (!data || typeof data !== "object") throw new Error("服务器返回数据格式异常");
        return data;
      });
    });
  }

  /* ---------- 状态拉取 ---------- */

  function applyState(data) {
    var rawItems = Array.isArray(data.items) ? data.items : [];
    var items = rawItems.filter(function (item) {
      return item && typeof item.id === "string" && item.id;
    });
    items.sort(function (a, b) {
      return timeValue(b.created_at) - timeValue(a.created_at);
    });
    state.token = typeof data.token === "string" ? data.token : "";
    state.items = items;
    state.updatedAt = typeof data.updated_at === "string" ? data.updated_at : "";
    state.loaded = true;
    renderAll();
  }

  function loadState(options) {
    var opts = options || {};
    if (inFlight) {
      pendingReload = true;
      return;
    }
    inFlight = true;
    pendingReload = false;
    if (!opts.silent) {
      refreshBtn.disabled = true;
      refreshBtn.textContent = "刷新中…";
    }
    fetchJson("api/state").then(function (data) {
      applyState(data);
      if (!opts.silent) setFeedback("已刷新", false);
    }).catch(function (err) {
      var reason = friendlyError(err);
      if (!state.loaded) {
        statsEl.textContent = "加载失败";
        renderAgentLinkFailure();
        renderLoadError(reason);
      } else if (opts.silent) {
        setFeedback("自动刷新失败：" + reason, true);
      } else {
        setFeedback("刷新失败：" + reason, true);
      }
    }).then(function () {
      inFlight = false;
      if (!opts.silent) {
        refreshBtn.disabled = false;
        refreshBtn.textContent = "刷新";
      }
      if (pendingReload) {
        pendingReload = false;
        loadState({ silent: true });
        return;
      }
      schedulePoll();
    });
  }

  /* ---------- 渲染：统计 / 链接 / 状态区 ---------- */

  function renderAll() {
    renderStats();
    renderAgentLink();
    renderStateArea();
    renderItems();
    renderUploadStatuses();
    schedulePoll();
  }

  function renderStats() {
    var count = state.items.length;
    var updated = formatTime(state.updatedAt);
    if (!updated && state.items.length) {
      updated = formatTime(state.items[0].created_at);
    }
    statsEl.textContent = "共 " + count + " 份资料 · 更新于 " + (updated || "—");
  }

  function renderAgentLink() {
    var link = indexLink();
    if (link) {
      agentLinkEl.textContent = link;
      agentLinkEl.title = link;
      copyAgentBtn.disabled = false;
      copyTopBtn.disabled = false;
    } else if (state.loaded) {
      agentLinkEl.textContent = "暂不可用（未获取到访问令牌）";
      agentLinkEl.title = "";
      copyAgentBtn.disabled = true;
      copyTopBtn.disabled = true;
    } else {
      agentLinkEl.textContent = "加载中…";
      agentLinkEl.title = "";
      copyAgentBtn.disabled = true;
      copyTopBtn.disabled = true;
    }
  }

  function renderAgentLinkFailure() {
    agentLinkEl.textContent = "加载失败，请点击刷新重试";
    agentLinkEl.title = "";
    copyAgentBtn.disabled = true;
    copyTopBtn.disabled = true;
  }

  function renderStateArea() {
    if (!state.loaded) return;
    stateArea.textContent = "";
    if (state.items.length === 0) {
      stateArea.appendChild(makeEl("p", "empty", "还没有资料。点击上方区域上传第一个文件。"));
    }
  }

  function renderLoadError(reason) {
    stateArea.textContent = "";
    var box = makeEl("div", "notice");
    box.appendChild(makeEl("p", "notice-text", "加载失败：" + reason));
    box.appendChild(makeEl("p", "hint", "请确认服务已启动，然后点击重试。"));
    var retry = makeEl("button", "btn btn-sm", "重试");
    retry.type = "button";
    retry.addEventListener("click", function () {
      renderStateLoading();
      loadState({ silent: false });
    });
    box.appendChild(retry);
    stateArea.appendChild(box);
  }

  function renderStateLoading() {
    stateArea.textContent = "";
    stateArea.appendChild(makeEl("p", "empty", "正在加载资料…"));
  }

  /* ---------- 渲染：资料列表 ---------- */

  function renderItems() {
    var prev = null;
    var seen = {};
    var index;
    for (index = 0; index < state.items.length; index += 1) {
      var item = state.items[index];
      seen[item.id] = true;
      var refs = itemNodes[item.id];
      if (!refs) {
        refs = createItemRefs(item);
        itemNodes[item.id] = refs;
      }
      updateItemRefs(refs, item);
      if (!prev) {
        if (itemListEl.firstChild !== refs.li) itemListEl.insertBefore(refs.li, itemListEl.firstChild);
      } else if (refs.li.previousSibling !== prev) {
        itemListEl.insertBefore(refs.li, prev.nextSibling);
      }
      prev = refs.li;
    }
    var ids = Object.keys(itemNodes);
    for (index = 0; index < ids.length; index += 1) {
      if (!seen[ids[index]]) {
        var dead = itemNodes[ids[index]];
        if (dead.li.parentNode) dead.li.parentNode.removeChild(dead.li);
        delete itemNodes[ids[index]];
      }
    }
  }

  function createItemRefs(item) {
    var li = makeEl("li", "item");
    var inner = makeEl("div", "item-inner");
    var media = makeEl("div", "item-media");
    var body = makeEl("div", "item-body");
    inner.appendChild(media);
    inner.appendChild(body);
    li.appendChild(inner);

    var head = makeEl("div", "item-head");
    var name = makeEl("h3", "item-name");
    var tag = makeEl("span", "kind-tag");
    head.appendChild(name);
    head.appendChild(tag);

    var meta = makeEl("p", "item-meta");
    var desc = makeEl("p", "item-desc");
    var error = makeEl("p", "item-error");
    error.hidden = true;
    var player = makeEl("div", "item-player");
    player.hidden = true;

    var actions = makeEl("div", "item-actions");
    var copyBtn = makeEl("button", "btn btn-sm", "复制链接");
    copyBtn.type = "button";
    copyBtn.title = "复制这一份资料的详情链接";
    var download = makeEl("a", "btn btn-sm", "下载");
    var redescBtn = makeEl("button", "btn btn-sm", "重新生成描述");
    redescBtn.type = "button";
    redescBtn.title = "只重新生成描述，原始文件保持不变";
    var delBtn = makeEl("button", "btn btn-sm", "删除");
    delBtn.type = "button";
    delBtn.title = "删除这份资料";
    actions.appendChild(copyBtn);
    actions.appendChild(download);
    actions.appendChild(redescBtn);
    actions.appendChild(delBtn);

    body.appendChild(head);
    body.appendChild(meta);
    body.appendChild(desc);
    body.appendChild(error);
    body.appendChild(player);
    body.appendChild(actions);

    var refs = {
      li: li,
      media: media,
      name: name,
      tag: tag,
      meta: meta,
      desc: desc,
      error: error,
      player: player,
      copyBtn: copyBtn,
      download: download,
      redescBtn: redescBtn,
      delBtn: delBtn,
      itemId: item.id,
      itemName: "",
      thumb: null,
      thumbSrc: "",
      playerKind: "",
      playerEl: null
    };

    copyBtn.addEventListener("click", function () {
      copyAndReport(itemLink(refs.itemId), "已复制「" + refs.itemName + "」的链接");
    });
    redescBtn.addEventListener("click", function () {
      redescribeItem(refs);
    });
    delBtn.addEventListener("click", function () {
      removeItem(refs);
    });

    return refs;
  }

  function updateItemRefs(refs, item) {
    refs.itemId = item.id;
    refs.itemName = typeof item.name === "string" && item.name ? item.name : "未命名文件";
    /* 超长文件名（如微信保存的图片）只显示前 48 字，完整名保留在 title */
    var displayedName = refs.itemName.length > 48
      ? refs.itemName.slice(0, 48) + "…"
      : refs.itemName;
    setText(refs.name, displayedName);
    refs.name.title = refs.itemName;

    var kind = normalizeKind(item.kind);
    setText(refs.tag, KIND_LABEL[kind]);
    setText(refs.meta, formatSize(item.size) + " · " + (formatTime(item.created_at) || "时间未知"));

    var status = item.status === "processing" || item.status === "error" ? item.status : "ready";
    var description = typeof item.description === "string" ? item.description : "";
    var errorText = typeof item.error === "string" ? item.error : "";

    refs.error.hidden = true;
    refs.error.textContent = "";
    refs.desc.className = "item-desc";
    if (status === "processing") {
      refs.desc.className = "item-desc is-processing";
      setText(refs.desc, "正在生成描述…");
    } else if (status === "error") {
      setText(refs.desc, description ? description : "（暂无描述）");
      refs.error.hidden = false;
      refs.error.textContent = "描述生成失败：" + (errorText || "未知原因") + "。可删除后重新上传该文件。";
    } else {
      setText(refs.desc, description ? description : "（暂无描述）");
    }

    /* 缩略图（仅图片） */
    if (kind === "image") {
      refs.media.hidden = false;
      var src = rawLink(item.id);
      if (!refs.thumb) {
        refs.thumb = document.createElement("img");
        refs.thumb.loading = "lazy";
        refs.thumb.decoding = "async";
        refs.thumb.addEventListener("error", function () {
          refs.media.hidden = true;
        });
        refs.media.appendChild(refs.thumb);
      }
      if (refs.thumbSrc !== src) {
        refs.thumbSrc = src;
        refs.thumb.src = src;
      }
      refs.thumb.alt = "资料预览：" + refs.itemName;
    } else {
      refs.media.hidden = true;
      if (refs.thumb) {
        if (refs.thumb.parentNode) refs.thumb.parentNode.removeChild(refs.thumb);
        refs.thumb = null;
        refs.thumbSrc = "";
      }
    }

    /* 音频 / 视频播放器（原生控件） */
    if (kind === "audio" || kind === "video") {
      if (refs.playerKind !== kind) {
        refs.player.textContent = "";
        var mediaEl = document.createElement(kind === "audio" ? "audio" : "video");
        mediaEl.controls = true;
        mediaEl.preload = "none";
        mediaEl.src = rawLink(item.id);
        mediaEl.setAttribute("aria-label", "播放：" + refs.itemName);
        refs.player.appendChild(mediaEl);
        refs.playerEl = mediaEl;
        refs.playerKind = kind;
      }
      refs.player.hidden = false;
    } else if (refs.playerKind) {
      refs.player.textContent = "";
      refs.playerKind = "";
      refs.playerEl = null;
      refs.player.hidden = true;
    }

    refs.download.href = rawLink(item.id);
    refs.download.setAttribute("download", refs.itemName);
    refs.download.title = "下载原始文件";
    refs.redescBtn.disabled = status === "processing";
  }

  function redescribeItem(refs) {
    if (refs.redescBtn.disabled) return;
    refs.redescBtn.disabled = true;
    setFeedback("正在重新生成「" + refs.itemName + "」的描述…", false);
    fetchJson("api/items/" + encodeURIComponent(refs.itemId) + "/redescribe", "POST")
      .then(function () {
        loadState({ silent: true });
      })
      .catch(function (err) {
        refs.redescBtn.disabled = false;
        setFeedback("重新生成失败：" + friendlyError(err), true);
      });
  }

  function removeItem(refs) {
    var name = refs.itemName;
    if (!window.confirm("确定删除「" + name + "」吗？删除后无法恢复。")) return;
    refs.delBtn.disabled = true;
    fetchJson("api/items/" + encodeURIComponent(refs.itemId), "DELETE").then(function () {
      setFeedback("已删除「" + name + "」", false);
      state.items = state.items.filter(function (item) {
        return item.id !== refs.itemId;
      });
      if (itemNodes[refs.itemId]) {
        if (itemNodes[refs.itemId].li.parentNode) {
          itemNodes[refs.itemId].li.parentNode.removeChild(itemNodes[refs.itemId].li);
        }
        delete itemNodes[refs.itemId];
      }
      renderStats();
      renderStateArea();
      renderUploadStatuses();
      schedulePoll();
      loadState({ silent: true });
    }).catch(function (err) {
      refs.delBtn.disabled = false;
      setFeedback("删除失败：" + friendlyError(err), true);
    });
  }

  /* ---------- 渲染：上传队列 ---------- */

  function findItem(id) {
    if (!id) return null;
    for (var index = 0; index < state.items.length; index += 1) {
      if (state.items[index].id === id) return state.items[index];
    }
    return null;
  }

  function addFiles(fileList) {
    if (!fileList || !fileList.length) return;
    var added = 0;
    for (var index = 0; index < fileList.length; index += 1) {
      var file = fileList[index];
      if (!file || typeof file.name !== "string") continue;
      var entry = {
        file: file,
        phase: "queued",
        percent: 0,
        itemId: "",
        error: "",
        node: null
      };
      uploads.push(entry);
      createUploadNode(entry);
      added += 1;
    }
    if (!added) return;
    setFeedback("已加入上传队列：" + added + " 个文件", false);
    pumpQueue();
  }

  function createUploadNode(entry) {
    var li = makeEl("li", "upload");
    var top = makeEl("div", "upload-top");
    var shown = entry.file.name.length > 48
      ? entry.file.name.slice(0, 48) + "…"
      : entry.file.name;
    var name = makeEl("span", "upload-name", shown);
    name.title = entry.file.name;
    var status = makeEl("span", "upload-status", "排队中");
    top.appendChild(name);
    top.appendChild(status);

    var progress = makeEl("div", "progress");
    progress.setAttribute("role", "progressbar");
    progress.setAttribute("aria-valuemin", "0");
    progress.setAttribute("aria-valuemax", "100");
    progress.setAttribute("aria-valuenow", "0");
    progress.setAttribute("aria-label", entry.file.name + " 上传进度");
    var bar = makeEl("div", "progress-bar");
    progress.appendChild(bar);

    li.appendChild(top);
    li.appendChild(progress);
    uploadsEl.appendChild(li);

    entry.node = {
      li: li,
      status: status,
      progress: progress,
      bar: bar,
      percent: -1
    };
  }

  function updateUploadNode(entry) {
    var node = entry.node;
    if (!node) return;
    var isError = false;
    var text;
    var percent = entry.percent;
    if (entry.phase === "queued") {
      text = "排队中";
      percent = 0;
    } else if (entry.phase === "uploading") {
      text = percent === null ? "上传中…" : "上传中 " + percent + "%";
    } else if (entry.phase === "failed") {
      text = "上传失败：" + (entry.error || "未知原因");
      isError = true;
    } else {
      percent = 100;
      var item = findItem(entry.itemId);
      if (!item) {
        text = "已上传 · 等待生成描述…";
      } else if (item.status === "ready") {
        text = "已上传 · 描述已生成";
      } else if (item.status === "error") {
        text = "已上传 · 描述生成失败：" + (item.error || "未知原因");
        isError = true;
      } else {
        text = "已上传 · 描述生成中…";
      }
    }
    setText(node.status, text);
    node.li.className = isError ? "upload is-error" : "upload";
    var shown = percent === null ? 0 : percent;
    if (node.percent !== shown) {
      node.percent = shown;
      node.bar.style.width = shown + "%";
      node.progress.setAttribute("aria-valuenow", String(shown));
    }
  }

  function renderUploadStatuses() {
    for (var index = 0; index < uploads.length; index += 1) {
      updateUploadNode(uploads[index]);
    }
  }

  function pumpQueue() {
    if (activeUpload) return;
    for (var index = 0; index < uploads.length; index += 1) {
      if (uploads[index].phase === "queued") {
        startUpload(uploads[index]);
        return;
      }
    }
  }

  function finishUpload() {
    activeUpload = null;
    renderUploadStatuses();
    pumpQueue();
  }

  function failUpload(entry, message) {
    entry.phase = "failed";
    entry.error = message;
    updateUploadNode(entry);
  }

  function startUpload(entry) {
    activeUpload = entry;
    entry.phase = "uploading";
    entry.percent = 0;
    entry.error = "";
    updateUploadNode(entry);

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "api/items?name=" + encodeURIComponent(entry.file.name));
    xhr.upload.onprogress = function (event) {
      if (event.lengthComputable && event.total > 0) {
        entry.percent = Math.max(0, Math.min(100, Math.round((event.loaded / event.total) * 100)));
      } else {
        entry.percent = null;
      }
      updateUploadNode(entry);
    };
    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 300) {
        var data = parseJson(xhr.responseText);
        var item = data && data.item ? data.item : null;
        if (item && typeof item.id === "string" && item.id) {
          entry.phase = "uploaded";
          entry.percent = 100;
          entry.itemId = item.id;
          updateUploadNode(entry);
          setFeedback("已上传「" + entry.file.name + "」，正在生成描述…", false);
          loadState({ silent: true });
        } else {
          failUpload(entry, "服务器返回数据格式异常");
        }
      } else {
        var dataErr = parseJson(xhr.responseText);
        failUpload(entry, dataErr && typeof dataErr.error === "string" ? dataErr.error : "HTTP " + xhr.status);
      }
      finishUpload();
    };
    xhr.onerror = function () {
      failUpload(entry, "网络错误，上传失败");
      finishUpload();
    };
    xhr.ontimeout = function () {
      failUpload(entry, "上传超时");
      finishUpload();
    };
    xhr.onabort = function () {
      failUpload(entry, "上传已取消");
      finishUpload();
    };
    xhr.send(entry.file);
  }

  /* ---------- 轮询 ---------- */

  function needsPoll() {
    for (var index = 0; index < state.items.length; index += 1) {
      if (state.items[index] && state.items[index].status === "processing") return true;
    }
    return false;
  }

  function schedulePoll() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (!needsPoll() || document.hidden) return;
    pollTimer = setTimeout(function () {
      pollTimer = null;
      loadState({ silent: true });
    }, POLL_MS);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      return;
    }
    if (needsPoll()) loadState({ silent: true });
  });

  /* ---------- 事件绑定 ---------- */

  copyTopBtn.addEventListener("click", function () {
    copyAndReport(indexLink(), "已复制智能体链接");
  });
  copyAgentBtn.addEventListener("click", function () {
    copyAndReport(indexLink(), "已复制智能体链接");
  });

  refreshBtn.addEventListener("click", function () {
    if (refreshBtn.disabled) return;
    loadState({ silent: false });
  });

  dropzone.addEventListener("click", function () {
    fileInput.click();
  });
  dropzone.addEventListener("keydown", function (event) {
    if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
      event.preventDefault();
      fileInput.click();
    }
  });

  fileInput.addEventListener("change", function () {
    addFiles(fileInput.files);
    /* 清空 input，允许再次选择同一文件 */
    fileInput.value = "";
  });

  dropzone.addEventListener("dragenter", function (event) {
    event.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragover", function (event) {
    event.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragleave", function (event) {
    if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove("dragover");
  });
  dropzone.addEventListener("drop", function (event) {
    event.preventDefault();
    dropzone.classList.remove("dragover");
    if (event.dataTransfer && event.dataTransfer.files) addFiles(event.dataTransfer.files);
  });

  /* 拖到页面其它位置时不要让浏览器直接打开文件 */
  document.addEventListener("dragover", function (event) {
    event.preventDefault();
  });
  document.addEventListener("drop", function (event) {
    event.preventDefault();
  });

  /* ---------- 启动 ---------- */

  renderStateLoading();
  renderAgentLink();
  loadState({ silent: true });
})();

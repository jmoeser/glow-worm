(function () {
    var sortKey = "next_due_date";
    var sortDir = "asc";
    var pendingCloseFocus = null;
    var pageInert = [];
    var drawerWide = false;

    function wideDrawer() {
        return window.matchMedia("(min-width: 801px)").matches;
    }

    function focusables(root) {
        return Array.prototype.filter.call(
            root.querySelectorAll(
                "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
            ),
            function (el) {
                if (el.closest("[hidden]")) return false;
                if (el.type === "hidden") return false;
                var details = el.closest("details");
                if (details && !details.open && el.tagName !== "SUMMARY") return false;
                return true;
            }
        );
    }

    function clearPageInert() {
        for (var i = 0; i < pageInert.length; i++) {
            pageInert[i].removeAttribute("inert");
        }
        pageInert = [];
        var sidebar = document.getElementById("gw-sidebar");
        if (!sidebar) return;
        sidebar.removeAttribute("inert");
        if (window.matchMedia("(max-width: 800px)").matches && !sidebar.classList.contains("is-open")) {
            sidebar.setAttribute("inert", "");
        }
    }

    function holdPageInert() {
        clearPageInert();
        var drawer = document.getElementById("bill-drawer");
        var backdrop = document.getElementById("bill-drawer-backdrop");
        var main = document.querySelector("main.gw-main");
        var sidebar = document.getElementById("gw-sidebar");
        if (sidebar) sidebar.setAttribute("inert", "");
        if (!drawer || !main) return;
        var node = drawer;
        while (node && node !== main) {
            var parent = node.parentElement;
            if (!parent) break;
            for (var i = 0; i < parent.children.length; i++) {
                var child = parent.children[i];
                if (child === node || child === backdrop || child.contains(drawer)) continue;
                child.setAttribute("inert", "");
                pageInert.push(child);
            }
            node = parent;
        }
    }

    function syncDrawerChrome(moveFocus) {
        var drawer = document.getElementById("bill-drawer");
        if (!drawer) return;
        var id = drawer.getAttribute("data-bill-id");
        var backdrop = document.getElementById("bill-drawer-backdrop");
        var open = !drawer.hidden && drawer.querySelector("form[data-bill-id]");
        if (wideDrawer() && open) {
            drawer.setAttribute("role", "dialog");
            drawer.setAttribute("aria-modal", "true");
            if (id) drawer.setAttribute("aria-labelledby", "bill-editor-title-" + id);
            if (backdrop) backdrop.hidden = false;
            holdPageInert();
            if (moveFocus && id) {
                var name = document.getElementById("edit-name-" + id);
                if (name) name.focus();
            }
        } else {
            drawer.removeAttribute("role");
            drawer.removeAttribute("aria-modal");
            drawer.removeAttribute("aria-labelledby");
            if (backdrop) backdrop.hidden = true;
            clearPageInert();
        }
        if (window.syncPageOverflow) window.syncPageOverflow();
    }

    var sortAttr = {
        next_due_date: "data-sort-next-due",
        name: "data-sort-name",
        amount: "data-sort-amount",
        paid_from: "data-sort-paid-from",
        paid_with: "data-sort-paid-with",
        provider: "data-sort-provider",
        frequency: "data-sort-frequency"
    };

    function rowId(row) {
        return parseInt(String(row.id || "").replace("bill-row-", ""), 10) || 0;
    }

    function compareRows(a, b) {
        var attr = sortAttr[sortKey] || "data-sort-next-due";
        var av = a.getAttribute(attr) || "";
        var bv = b.getAttribute(attr) || "";
        var cmp = sortKey === "amount"
            ? (parseFloat(av) || 0) - (parseFloat(bv) || 0)
            : av.localeCompare(bv);
        if (cmp === 0 && sortKey !== "next_due_date") {
            cmp = (a.getAttribute("data-sort-next-due") || "").localeCompare(
                b.getAttribute("data-sort-next-due") || ""
            );
        }
        if (cmp === 0) {
            cmp = rowId(a) - rowId(b);
        }
        return sortDir === "asc" ? cmp : -cmp;
    }

    function applySort() {
        var list = document.getElementById("bills-rows");
        if (!list) return;
        var rows = Array.prototype.slice.call(list.querySelectorAll(":scope > .gw-bill-row"));
        rows.sort(compareRows);
        rows.forEach(function (row) {
            list.appendChild(row);
        });
        var drawer = document.getElementById("bill-drawer");
        if (drawer && !drawer.hidden) {
            placeEditor();
        }
    }

    function placeEditor() {
        var drawer = document.getElementById("bill-drawer");
        if (!drawer || drawer.hidden) return;
        var rows = document.querySelectorAll("#bills-rows > .gw-bill-row");
        for (var i = 0; i < rows.length; i++) {
            rows[i].style.marginBottom = "";
        }
        drawer.style.position = "";
        drawer.style.left = "";
        drawer.style.width = "";
        drawer.style.zIndex = "";
        if (wideDrawer()) {
            drawer.style.top = "";
            return;
        }
        var stack = document.getElementById("bills-stack");
        var id = drawer.getAttribute("data-bill-id");
        var row = id && document.getElementById("bill-row-" + id);
        if (!stack || !row) {
            drawer.style.top = "";
            return;
        }
        var top = row.getBoundingClientRect().bottom - stack.getBoundingClientRect().top;
        drawer.style.top = top + "px";
        row.style.marginBottom = (drawer.offsetHeight + 8) + "px";
    }

    function closeEditor(options) {
        options = options || {};
        var drawer = document.getElementById("bill-drawer");
        if (!drawer) return;
        var id = drawer.getAttribute("data-bill-id");
        drawer.innerHTML = "";
        drawer.hidden = true;
        drawer.removeAttribute("data-bill-id");
        drawer.style.top = "";
        drawer.style.position = "";
        drawer.style.left = "";
        drawer.style.width = "";
        drawer.style.zIndex = "";
        drawer.removeAttribute("role");
        drawer.removeAttribute("aria-modal");
        drawer.removeAttribute("aria-labelledby");
        var backdrop = document.getElementById("bill-drawer-backdrop");
        if (backdrop) backdrop.hidden = true;
        clearPageInert();
        var rows = document.querySelectorAll(".gw-bill-row");
        for (var i = 0; i < rows.length; i++) {
            rows[i].style.marginBottom = "";
        }
        if (id) {
            var button = document.getElementById("bill-edit-" + id);
            if (button) {
                button.textContent = "Edit";
                button.setAttribute("aria-expanded", "false");
            }
        }
        if (window.syncPageOverflow) window.syncPageOverflow();
        if (options.fromSwap) {
            if (id) pendingCloseFocus = id;
            return;
        }
        if (!id) return;
        var focusButton = document.getElementById("bill-edit-" + id);
        if (focusButton) {
            focusButton.focus();
        } else {
            var sort = document.getElementById("bills-sort");
            if (sort) sort.focus();
        }
    }
    window.closeEditor = closeEditor;

    function gwPaidFromChanged(select) {
        if (!select) { return; }
        var root = select.closest("form") || select.closest("#bill-drawer") || select.closest("tr");
        if (!root) { return; }
        var value = select.value || "";
        var isBills = value === "bills_fund";
        var isFund = value.indexOf("fund:") === 0;
        var typeWrap = root.querySelector("[data-bill-type]");
        var typeSelect = typeWrap ? typeWrap.querySelector("select") : null;
        var help = root.querySelector("[data-confirm-help]");
        var catWrap = root.querySelector("[data-fund-category]");
        var note = root.querySelector("[data-overdue-note]");
        if (typeSelect) {
            typeSelect.style.display = isBills ? "" : "none";
            typeSelect.disabled = !isBills;
        }
        if (help) { help.style.display = isBills ? "none" : ""; }
        if (catWrap) {
            catWrap.style.display = isFund ? "" : "none";
            var catSelect = catWrap.querySelector("select");
            if (catSelect) { catSelect.disabled = !isFund; }
        }
        if (note) {
            if (!isBills) {
                note.textContent = "";
            } else {
                var billType = typeSelect && !typeSelect.disabled
                    ? typeSelect.value
                    : (select.getAttribute("data-bill-type") || "fixed");
                note.textContent = billType === "variable"
                    ? "Overdue payments will wait in Due to confirm."
                    : "Overdue payments will then be paid from the Bills fund automatically.";
            }
        }
    }
    window.gwPaidFromChanged = gwPaidFromChanged;

    document.addEventListener("click", function (event) {
        var button = event.target.closest && event.target.closest("button[aria-controls='bill-drawer']");
        if (!button || button.getAttribute("aria-expanded") !== "true") return;
        event.preventDefault();
        event.stopImmediatePropagation();
        closeEditor();
    }, true);

    document.addEventListener("click", function (event) {
        if (event.target && event.target.id === "bill-drawer-backdrop") closeEditor();
    });

    document.addEventListener("keydown", function (event) {
        var drawer = document.getElementById("bill-drawer");
        if (!drawer || !drawer.querySelector("form[data-bill-id]")) return;
        if (event.key === "Escape") {
            var sidebar = document.getElementById("gw-sidebar");
            var narrow = window.matchMedia("(max-width: 800px)").matches;
            if (narrow && sidebar && sidebar.classList.contains("is-open")) return;
            event.preventDefault();
            closeEditor();
            return;
        }
        if (event.key !== "Tab" || drawer.getAttribute("aria-modal") !== "true") return;
        var items = focusables(drawer);
        if (!items.length) return;
        var first = items[0];
        var last = items[items.length - 1];
        var active = document.activeElement;
        if (event.shiftKey && (active === first || !drawer.contains(active))) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && (active === last || !drawer.contains(active))) {
            event.preventDefault();
            first.focus();
        }
    }, true);

    document.addEventListener("toggle", function (event) {
        if (event.target.closest && event.target.closest("#bill-drawer")) {
            placeEditor();
        }
    }, true);

    function syncOpenDrawer() {
        var drawer = document.getElementById("bill-drawer");
        var open = drawer && !drawer.hidden && drawer.querySelector("form[data-bill-id]");
        var nowWide = wideDrawer();
        if (!open) {
            drawerWide = nowWide;
            return;
        }
        var crossedUp = nowWide && !drawerWide;
        drawerWide = nowWide;
        syncDrawerChrome(crossedUp);
        placeEditor();
    }

    window.addEventListener("resize", syncOpenDrawer);
    // The nav registers its 800px listener on DOMContentLoaded, before this one,
    // and that listener clears the sidebar lock when the viewport grows.
    // Re-apply after it so an open drawer keeps the sidebar inert.
    document.addEventListener("DOMContentLoaded", function () {
        var navBreakpoint = window.matchMedia("(max-width: 800px)");
        if (navBreakpoint.addEventListener) {
            navBreakpoint.addEventListener("change", syncOpenDrawer);
        } else if (navBreakpoint.addListener) {
            navBreakpoint.addListener(syncOpenDrawer);
        }
    });

    document.body.addEventListener("htmx:beforeSwap", function (event) {
        var xhr = event.detail.xhr;
        if (!xhr) return;
        var retarget = xhr.getResponseHeader("HX-Retarget");
        if (!retarget) return;
        if (retarget !== "#bill-editor-errors" && retarget.indexOf("#due-soon-errors-") !== 0) {
            return;
        }
        var node = document.querySelector(retarget);
        if (!node) return;
        event.detail.shouldSwap = true;
        event.detail.target = node;
    });

    document.body.addEventListener("htmx:afterSwap", function (event) {
        var target = event.detail.target;
        var xhr = event.detail.xhr;
        var focusSel = xhr && xhr.getResponseHeader("HX-Focus");
        if (focusSel) {
            var el = document.querySelector(focusSel);
            if (el) {
                var details = el.closest("details");
                if (details) details.open = true;
                el.focus();
            }
        } else if (
            target &&
            (target.id === "bill-editor-errors" || String(target.id || "").indexOf("due-soon-errors-") === 0)
        ) {
            target.focus();
        }
        if (!target) return;
        if (target.id === "bill-drawer") {
            var form = target.querySelector("form[data-bill-id]");
            if (!form) {
                closeEditor({ fromSwap: true });
                return;
            }
            var id = form.getAttribute("data-bill-id");
            target.setAttribute("data-bill-id", id);
            var openButtons = document.querySelectorAll("[aria-controls='bill-drawer'][aria-expanded='true']");
            for (var i = 0; i < openButtons.length; i++) {
                openButtons[i].textContent = "Edit";
                openButtons[i].setAttribute("aria-expanded", "false");
            }
            var button = document.getElementById("bill-edit-" + id);
            if (button) {
                button.textContent = "Close";
                button.setAttribute("aria-expanded", "true");
            }
            target.hidden = false;
            drawerWide = wideDrawer();
            syncDrawerChrome(false);
            placeEditor();
            var name = document.getElementById("edit-name-" + id);
            if (name) name.focus();
            return;
        }
        if (target.id === "bills-rows" || String(target.id || "").indexOf("bill-row-") === 0) {
            applySort();
        }
    });

    document.body.addEventListener("htmx:afterSettle", function () {
        if (pendingCloseFocus === null) return;
        var id = pendingCloseFocus;
        pendingCloseFocus = null;
        var button = document.getElementById("bill-edit-" + id);
        if (button) {
            button.focus();
        } else {
            var sort = document.getElementById("bills-sort");
            if (sort) sort.focus();
        }
    });

    document.body.addEventListener("gwBillSuccess", function (event) {
        var detail = event.detail || {};
        var message = detail.message || "";
        if (message === "Bill added.") {
            document.querySelectorAll(".gw-add-form[open]").forEach(function (details) {
                var form = details.querySelector("form");
                if (form) form.reset();
                details.removeAttribute("open");
            });
        }
        var box = document.getElementById("bills-message");
        if (box && message) {
            box.textContent = "";
            var note = document.createElement("p");
            note.className = "gw-msg-success";
            note.textContent = message;
            box.appendChild(note);
            box.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
        function setText(id, value) {
            var el = document.getElementById(id);
            if (el && value != null && value !== "") el.textContent = value;
        }
        if (detail.monthly) setText("bills-monthly-cost", "$" + detail.monthly);
        if (detail.annual) setText("bills-annual-cost", "$" + detail.annual);
        if (detail.other != null && detail.other !== "") setText("bills-other-cost", detail.other);
        setText("bills-essential-cost", detail.essential);
        setText("bills-optional-cost", detail.optional);
        setText("bills-unmarked-cost", detail.unmarked);
        if (detail.unmarked_count != null && detail.unmarked_count !== "") {
            setText("bills-unmarked-count", String(detail.unmarked_count));
        }
        if (detail.active_count != null && detail.waiting_count != null) {
            var count = document.getElementById("bills-count");
            if (count) {
                count.textContent = detail.active_count + " active payments · " + detail.waiting_count + " waiting to confirm";
            }
        }
        var drawer = document.getElementById("bill-drawer");
        if (
            drawer &&
            detail.billId != null &&
            String(detail.billId) === drawer.getAttribute("data-bill-id") &&
            !drawer.querySelector("form[data-bill-id]")
        ) {
            closeEditor({ fromSwap: true });
        }
    });

    function bindSort() {
        var select = document.getElementById("bills-sort");
        var reverse = document.getElementById("bills-sort-reverse");
        if (select) {
            select.addEventListener("change", function () {
                sortKey = select.value || "next_due_date";
                sortDir = "asc";
                if (reverse) reverse.setAttribute("aria-pressed", "false");
                applySort();
            });
        }
        if (reverse) {
            reverse.addEventListener("click", function () {
                sortDir = sortDir === "asc" ? "desc" : "asc";
                reverse.setAttribute("aria-pressed", sortDir === "desc" ? "true" : "false");
                applySort();
            });
        }
        applySort();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bindSort);
    } else {
        bindSort();
    }
})();

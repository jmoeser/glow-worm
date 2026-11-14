(function () {
    var sortKey = "next_due_date";
    var sortDir = "asc";
    var pendingCloseFocus = null;

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
        var stack = document.getElementById("bills-stack");
        if (!drawer || !stack || drawer.hidden) return;
        var id = drawer.getAttribute("data-bill-id");
        if (!id) return;
        var row = document.getElementById("bill-row-" + id);
        if (!row) return;
        var rows = document.querySelectorAll("#bills-rows > .gw-bill-row");
        for (var i = 0; i < rows.length; i++) {
            rows[i].style.marginBottom = "";
        }
        var top = row.getBoundingClientRect().bottom - stack.getBoundingClientRect().top;
        drawer.style.position = "absolute";
        drawer.style.left = "0";
        drawer.style.width = "100%";
        drawer.style.zIndex = "1";
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
        drawer.removeAttribute("role");
        drawer.removeAttribute("aria-modal");
        drawer.removeAttribute("aria-labelledby");
        var backdrop = document.getElementById("bill-drawer-backdrop");
        if (backdrop) backdrop.hidden = true;
        var main = document.querySelector("main.gw-main");
        if (main) main.removeAttribute("inert");
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

    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        var drawer = document.getElementById("bill-drawer");
        if (!drawer || !drawer.querySelector("form[data-bill-id]")) return;
        var sidebar = document.getElementById("gw-sidebar");
        var narrow = window.matchMedia("(max-width: 800px)").matches;
        if (narrow && sidebar && sidebar.classList.contains("is-open")) return;
        event.preventDefault();
        closeEditor();
    }, true);

    document.addEventListener("toggle", function (event) {
        if (event.target.closest && event.target.closest("#bill-drawer")) {
            placeEditor();
        }
    }, true);

    window.addEventListener("resize", function () {
        placeEditor();
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
            placeEditor();
            var name = document.getElementById("edit-name-" + id);
            if (name) name.focus();
            if (window.syncPageOverflow) window.syncPageOverflow();
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

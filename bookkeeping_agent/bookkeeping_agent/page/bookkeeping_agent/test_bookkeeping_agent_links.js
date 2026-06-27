const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "bookkeeping_agent.js"), "utf8");

function fakeDollar() {
	return {
		find: () => fakeDollar(),
		html: () => fakeDollar(),
		append: () => fakeDollar(),
	};
}

const context = {
	console,
	bookkeeping_agent: {},
	frappe: {
		pages: { "bookkeeping-agent": {} },
		provide(namespace) {
			namespace.split(".").reduce((target, key) => {
				target[key] = target[key] || {};
				return target[key];
			}, context);
		},
		ui: { make_app_page: () => {} },
		utils: {
			escape_html(value) {
				return String(value || "")
					.replace(/&/g, "&amp;")
					.replace(/</g, "&lt;")
					.replace(/>/g, "&gt;")
					.replace(/"/g, "&quot;")
					.replace(/'/g, "&#039;");
			},
			icon: () => "",
		},
	},
	__: (value) => value,
	$: fakeDollar,
	cint: (value) => Number.parseInt(value, 10) || 0,
	window: { location: { origin: "http://development.localhost:8000" } },
	URL,
	setTimeout,
	clearTimeout,
};

vm.createContext(context);
vm.runInContext(source, context, { filename: "bookkeeping_agent.js" });

const agent = Object.create(context.bookkeeping_agent.BookkeepingAgent.prototype);

const linkedText = agent.linkify_desk_routes(
	'Link: /app/payment-entry/ACC-PAY-2026-00006 and <code>/app/sales-invoice/ACC-SINV-2026-00007</code>'
);

assert.match(
	linkedText,
	/<a href="\/app\/payment-entry\/ACC-PAY-2026-00006" target="_blank" rel="noopener noreferrer">\/app\/payment-entry\/ACC-PAY-2026-00006<\/a>/
);
assert.match(
	linkedText,
	/<code><a href="\/app\/sales-invoice\/ACC-SINV-2026-00007" target="_blank" rel="noopener noreferrer">\/app\/sales-invoice\/ACC-SINV-2026-00007<\/a><\/code>/
);

const unsafeText = agent.linkify_desk_routes("Not linked: /api/method/x and javascript:alert(1)");
assert.equal(unsafeText, "Not linked: /api/method/x and javascript:alert(1)");

const existingLink = agent.linkify_desk_routes(
	'<a href="/app/payment-entry/ACC-PAY-2026-00006">Payment Entry</a>'
);
assert.equal(
	existingLink,
	'<a href="/app/payment-entry/ACC-PAY-2026-00006">Payment Entry</a>'
);

assert.equal(agent.should_open_href_in_new_tab("/app/payment-entry/ACC-PAY-2026-00006"), true);
assert.equal(agent.should_open_href_in_new_tab("https://example.com/help"), true);
assert.equal(agent.should_open_href_in_new_tab("mailto:accounts@example.com"), false);

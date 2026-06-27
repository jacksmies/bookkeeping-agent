frappe.provide("bookkeeping_agent");

frappe.pages["bookkeeping-agent"].on_page_load = function (wrapper) {
	frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Bookkeeping Agent"),
		single_column: true,
		hide_sidebar: true,
	});

	wrapper.bookkeeping_agent = new bookkeeping_agent.BookkeepingAgent(wrapper);
};

bookkeeping_agent.BookkeepingAgent = class BookkeepingAgent {
	constructor(wrapper) {
		this.wrapper = $(wrapper);
		this.page = wrapper.page;
		this.messages = [];
		this.pending_actions = [];
		this.tool_events = [];
		this.active_run_id = null;
		this.realtime_event_name = null;
		this.realtime_handler = null;
		this.is_loading = false;
		this.current_assistant_message = null;
		this.current_assistant_text = "";
		this.current_assistant_render_timer = null;
		this.has_streamed_text = false;
		this.latest_assistant_message = null;
		this.run_details_target = null;
		this.pending_actions_target = null;
		this.suggested_prompts = [
			{
				label: __("Overdue invoices"),
				prompt: __("Show overdue customer invoices for Demo Books LLC"),
				icon: "calendar-clock",
			},
			{
				label: __("Draft invoice"),
				prompt: __("Create a draft sales invoice for Acme Retail LLC in Demo Books LLC"),
				icon: "file-plus-2",
			},
			{
				label: __("General ledger"),
				prompt: __("Run the general ledger for Demo Books LLC this month"),
				icon: "book-open",
			},
			{
				label: __("Record payment"),
				prompt: __("Record a customer payment"),
				icon: "receipt",
			},
		];
		this.make();
	}

	make() {
		this.page.main.html(`
			<div class="bookkeeping-agent">
				<section class="bookkeeping-agent-chat" aria-label="${__("Bookkeeping chat")}">
					<div class="bookkeeping-agent-messages" role="log" aria-live="polite">
						<div class="bookkeeping-agent-welcome" data-empty-state>
							<div class="bookkeeping-agent-welcome-mark" aria-hidden="true">
								${this.icon("bot", "md")}
							</div>
							<div>
								<div class="bookkeeping-agent-kicker">${__("Bookkeeping agent")}</div>
								<div class="bookkeeping-agent-welcome-title">${__("Ask about invoices, ledgers, and payments")}</div>
								<div class="bookkeeping-agent-welcome-copy">
									${__("Read ERPNext accounting data, prepare draft actions, and review every write before it runs.")}
								</div>
							</div>
						</div>
					</div>

					${this.render_prompt_input()}
				</section>
			</div>
		`);

		this.$messages = this.wrapper.find(".bookkeeping-agent-messages");
		this.$empty_state = this.wrapper.find("[data-empty-state]");
		this.$composer = this.wrapper.find(".bookkeeping-agent-composer");
		this.$composer_hint = this.wrapper.find("[data-composer-hint]");
		this.$input = this.wrapper.find(".bookkeeping-agent-input");
		this.$send = this.wrapper.find(".bookkeeping-agent-send");
		this.$status = this.wrapper.find("[data-status]");

		this.bind_events();
		this.render_tool_trace();
		this.render_pending_actions();
		this.update_composer_state();
		this.$input.focus();
		this.resize_input();
	}

	render_prompt_input() {
		return `
			<div class="bookkeeping-agent-composer">
				<div class="bookkeeping-agent-input-shell">
					<textarea class="form-control bookkeeping-agent-input" rows="1" placeholder="${__(
						"Ask about invoices, payments, reports, or draft actions"
					)}"></textarea>
					<div class="bookkeeping-agent-composer-footer">
						<div class="bookkeeping-agent-prompts" aria-label="${__("Suggested prompts")}">
							${this.suggested_prompts
								.map((prompt, index) => {
									return `
										<button class="bookkeeping-agent-prompt" type="button" data-prompt-index="${index}">
											${this.icon(prompt.icon, "sm")}
											<span>${this.escape_html(prompt.label)}</span>
										</button>
									`;
								})
								.join("")}
						</div>
						<div class="bookkeeping-agent-composer-controls">
							<span class="bookkeeping-agent-status" data-status>${__("Ready")}</span>
							<button class="btn btn-primary icon-btn bookkeeping-agent-send" type="button" title="${__(
								"Send"
							)}" aria-label="${__("Send")}">
								${this.icon("send-horizontal", "sm")}
							</button>
						</div>
					</div>
				</div>
				<div class="bookkeeping-agent-composer-hint text-muted" data-composer-hint>
					${__("Enter to send. Shift + Enter for a new line.")}
				</div>
			</div>
		`;
	}

	bind_events() {
		this.$send.on("click", () => this.send_current_message());

		this.$input.on("keydown", (event) => {
			if (event.key === "Enter" && !event.shiftKey) {
				event.preventDefault();
				this.send_current_message();
			}
		});

		this.$input.on("input", () => this.resize_input());

		this.wrapper.find("[data-prompt-index]").on("click", (event) => {
			if (this.is_composer_disabled()) {
				return;
			}
			const index = cint($(event.currentTarget).attr("data-prompt-index"));
			const prompt = this.suggested_prompts[index];
			if (!prompt) {
				return;
			}
			this.$input.val(prompt.prompt);
			this.resize_input();
			this.send_current_message();
		});

		this.wrapper.on("click", "[data-confirm-action]", (event) => {
			const index = cint($(event.currentTarget).attr("data-confirm-action"));
			this.confirm_action(index);
		});

		this.wrapper.on("click", "[data-cancel-action]", (event) => {
			const index = cint($(event.currentTarget).attr("data-cancel-action"));
			this.pending_actions.splice(index, 1);
			this.render_pending_actions();
		});
	}

	send_current_message() {
		const text = (this.$input.val() || "").trim();
		if (!text || this.is_composer_disabled()) {
			return;
		}

		this.$input.val("");
		this.resize_input();
		this.add_user_message(text);
		this.call_agent();
	}

	add_user_message(text) {
		this.messages.push({ role: "user", content: text });
		this.render_message("user", text);
	}

	add_assistant_message(text, options = {}) {
		if (options.record !== false) {
			this.messages.push({ role: "assistant", content: text });
		}
		this.render_message("assistant", text, options);
	}

	render_message(role, text, options = {}) {
		this.clear_empty_state();

		const $message = this.render_conversation_message(role, options);

		this.$messages.append($message);
		if (options.loading) {
			this.render_loading_body($message);
		} else {
			this.update_message_body($message, role, text);
		}
		if (role === "assistant") {
			this.latest_assistant_message = $message;
		}
		this.scroll_messages_to_bottom();
		return $message;
	}

	render_conversation_message(role, options = {}) {
		const kind = options.kind ? ` bookkeeping-agent-message-${options.kind}` : "";
		const label = role === "user" ? __("You") : __("Bookkeeping Agent");
		const avatar =
			role === "assistant"
				? `<div class="bookkeeping-agent-avatar" aria-hidden="true">${this.icon("bot", "sm")}</div>`
				: "";

		return $(`
			<div class="bookkeeping-agent-message bookkeeping-agent-message-${role}${kind}" aria-label="${this.escape_html(label)}">
				${avatar}
				<div class="bookkeeping-agent-message-content">
					<div class="bookkeeping-agent-sr-only">${this.escape_html(label)}</div>
					<div class="bookkeeping-agent-message-body"></div>
				</div>
			</div>
		`);
	}

	render_loading_body($message) {
		$message.addClass("bookkeeping-agent-loading-message");
		$message.find(".bookkeeping-agent-message-body").html(`
			<span class="bookkeeping-agent-dot"></span>
			<span class="bookkeeping-agent-dot"></span>
			<span class="bookkeeping-agent-dot"></span>
		`);
	}

	update_message_body($message, role, text) {
		const html = role === "assistant" ? this.render_markdown(text) : this.render_plain_text(text);
		$message.removeClass("bookkeeping-agent-loading-message");
		$message.find(".bookkeeping-agent-message-body").html(html);
	}

	begin_live_assistant_message() {
		this.clear_stream_render_timer();
		this.current_assistant_text = "";
		this.has_streamed_text = false;
		this.current_assistant_message = this.render_message("assistant", "", {
			record: false,
			loading: true,
		});
		this.run_details_target = this.current_assistant_message;
		this.ensure_message_workflow(this.current_assistant_message);
	}

	ensure_live_assistant_message() {
		if (!this.current_assistant_message) {
			this.begin_live_assistant_message();
		}
		return this.current_assistant_message;
	}

	append_assistant_delta(delta) {
		if (!delta) {
			return;
		}
		this.ensure_live_assistant_message();
		this.has_streamed_text = true;
		this.current_assistant_text += delta;
		this.schedule_stream_render();
	}

	schedule_stream_render() {
		if (this.current_assistant_render_timer) {
			return;
		}

		this.current_assistant_render_timer = setTimeout(() => {
			this.current_assistant_render_timer = null;
			this.render_current_assistant_text(true);
		}, 35);
	}

	render_current_assistant_text(is_streaming) {
		const $message = this.ensure_live_assistant_message();
		this.update_message_body($message, "assistant", this.current_assistant_text || "");
		$message
			.find(".bookkeeping-agent-message-body")
			.toggleClass("is-streaming", Boolean(is_streaming));
		this.scroll_messages_to_bottom();
	}

	finalize_live_assistant_message(text, options = {}) {
		this.clear_stream_render_timer();
		const content = text || __("Done.");
		const $message = this.ensure_live_assistant_message();
		this.current_assistant_text = content;
		$message
			.toggleClass("bookkeeping-agent-message-error", options.kind === "error")
			.find(".bookkeeping-agent-message-body")
			.removeClass("is-streaming");
		this.update_message_body($message, "assistant", content);
		if (options.record !== false) {
			this.messages.push({ role: "assistant", content });
		}
		this.current_assistant_message = null;
		this.current_assistant_text = "";
		this.has_streamed_text = false;
		this.scroll_messages_to_bottom();
	}

	clear_stream_render_timer() {
		if (this.current_assistant_render_timer) {
			clearTimeout(this.current_assistant_render_timer);
			this.current_assistant_render_timer = null;
		}
	}

	async call_agent() {
		this.set_loading(true);
		const run_id = this.make_run_id();
		this.active_run_id = run_id;
		this.begin_live_assistant_message();
		this.reset_tool_trace();
		this.add_tool_event({
			id: "planning",
			label: __("Planning"),
			status: "running",
			message: __("Preparing the bookkeeping request."),
		});
		this.bind_realtime(run_id);

		try {
			const response = await frappe.call({
				method: "bookkeeping_agent.bookkeeping_agent.page.bookkeeping_agent.bookkeeping_agent.chat",
				type: "POST",
				args: {
					messages: JSON.stringify(this.messages),
					run_id,
				},
			});

			const data = response.message || {};
			if ((data.tool_results || []).length) {
				this.render_tool_trace(data.tool_results);
			}
			this.pending_actions = data.pending_actions || [];
			if (this.pending_actions.length) {
				this.pending_actions_target = this.current_assistant_message;
			}
			this.render_pending_actions();
			this.finalize_live_assistant_message(data.text || __("Done."));
		} catch (error) {
			const message = this.get_error_message(error);
			this.add_tool_event({
				id: `error-${Date.now()}`,
				label: __("Request failed"),
				status: "error",
				message,
			});
			this.finalize_live_assistant_message(message, {
				kind: "error",
				record: false,
			});
		} finally {
			this.unbind_realtime();
			this.active_run_id = null;
			this.set_loading(false);
			this.render_pending_actions();
			this.$input.focus();
		}
	}

	confirm_action(index) {
		const action = this.pending_actions[index];
		if (!action || this.is_loading) {
			return;
		}

		frappe.confirm(__("Run this action?"), async () => {
			this.set_loading(true);
			this.render_pending_actions();
			this.begin_live_assistant_message();
			this.reset_tool_trace();
			this.add_tool_event({
				id: "confirmed-action",
				label: __("Running action"),
				status: "running",
				message: action.summary || __("Executing approved ERPNext action."),
				pending_action: true,
			});
			try {
				const response = await frappe.call({
					method: "bookkeeping_agent.bookkeeping_agent.page.bookkeeping_agent.bookkeeping_agent.confirm_action",
					type: "POST",
					args: { action: JSON.stringify(action) },
					freeze: true,
					freeze_message: __("Running action"),
				});

				const data = response.message || {};
				const result = data.result || {};
				this.pending_actions.splice(index, 1);
				this.render_pending_actions();
				this.upsert_tool_event({
					id: "confirmed-action",
					label: __("Confirmed action"),
					status: "completed",
					message: data.text || __("Action completed."),
				});
				this.finalize_live_assistant_message(data.text || __("Action completed."));
				this.render_result_link(result.summary);
			} catch (error) {
				this.upsert_tool_event({
					id: "confirmed-action",
					label: __("Action failed"),
					status: "error",
					message: this.get_error_message(error),
				});
				this.finalize_live_assistant_message(this.get_error_message(error), {
					kind: "error",
					record: false,
				});
			} finally {
				this.set_loading(false);
				this.render_pending_actions();
			}
		});
	}

	make_run_id() {
		if (window.crypto && window.crypto.randomUUID) {
			return window.crypto.randomUUID();
		}
		return `run-${Date.now()}-${Math.random().toString(36).slice(2)}`;
	}

	bind_realtime(run_id) {
		if (!frappe.realtime || !frappe.realtime.on) {
			return;
		}

		this.unbind_realtime();
		this.realtime_event_name = `bookkeeping_agent_run:${run_id}`;
		this.realtime_handler = (data) => {
			if (this.active_run_id !== run_id) {
				return;
			}
			this.handle_realtime_event(data || {});
		};
		frappe.realtime.on(this.realtime_event_name, this.realtime_handler);
	}

	unbind_realtime() {
		if (this.realtime_event_name && frappe.realtime && frappe.realtime.off) {
			frappe.realtime.off(this.realtime_event_name, this.realtime_handler);
		}
		this.realtime_event_name = null;
		this.realtime_handler = null;
	}

	handle_realtime_event(data) {
		if (data.event === "assistant_started") {
			this.ensure_live_assistant_message();
			return;
		}

		if (data.event === "assistant_delta") {
			this.append_assistant_delta(data.delta || data.text || "");
			return;
		}

		if (data.event === "assistant_done") {
			if (data.text) {
				this.current_assistant_text = data.text;
				this.render_current_assistant_text(false);
			}
			return;
		}

		if (data.event === "status") {
			this.upsert_tool_event({
				id: "planning",
				label: data.label || __("Planning"),
				status: data.status || "running",
				message: data.message || __("Working."),
			});
			return;
		}

		if (data.event === "tool_started" || data.event === "tool_finished") {
			this.upsert_tool_event(data);
			return;
		}

		if (data.event === "error") {
			this.add_tool_event({
				id: `error-${Date.now()}`,
				label: data.label || __("Request failed"),
				status: "error",
				message: data.message || __("The request could not be completed."),
			});
			return;
		}

		if (data.event === "done") {
			this.upsert_tool_event({
				id: "done",
				label: data.label || __("Done"),
				status: data.status || "ok",
				message: data.message || __("Response ready."),
			});
		}
	}

	reset_tool_trace() {
		this.tool_events = [];
		this.render_tool_trace();
	}

	add_tool_event(event) {
		const next_event = this.normalize_tool_event(event);
		this.tool_events.push(next_event);
		this.render_tool_trace();
	}

	upsert_tool_event(event) {
		const next_event = this.normalize_tool_event(event);
		const index = this.tool_events.findIndex((current) => current.id === next_event.id);
		if (index === -1) {
			this.tool_events.push(next_event);
		} else {
			this.tool_events[index] = Object.assign({}, this.tool_events[index], next_event);
		}
		this.render_tool_trace();
	}

	normalize_tool_event(event) {
		return {
			id: event.id || `${event.tool || event.label || "event"}-${Date.now()}`,
			tool: event.tool || "",
			label: event.label || event.tool || __("Run status"),
			status: event.status || "ok",
			message: event.message || "",
			arguments: event.arguments || {},
			pending_action: Boolean(event.pending_action),
		};
	}

	render_tool_trace(events) {
		if (events) {
			this.tool_events = events.map((event) => this.normalize_tool_event(event));
		}

		const $target = this.run_details_target || this.current_assistant_message || this.latest_assistant_message;
		if (!$target || !$target.length) {
			return;
		}

		const $workflow = this.ensure_message_workflow($target);
		const $run = $workflow.find("[data-run-details]");

		if (!this.tool_events.length) {
			$run.empty();
			return;
		}

		const tool_count = this.tool_events.filter((event) => event.tool).length;
		const has_running = this.tool_events.some((event) => event.status === "running");
		const has_error = this.tool_events.some((event) => event.status === "error");
		const summary = this.get_run_summary(tool_count, has_running, has_error);
		const status_color = has_error ? "red" : has_running ? "blue" : "green";

		$run.html(
			this.render_workflow_timeline(this.tool_events, {
				summary,
				status_color,
				has_running,
				has_error,
			})
		);
		this.scroll_messages_to_bottom();
	}

	render_workflow_timeline(events, meta = {}) {
		const open_attr = meta.has_running || meta.has_error ? " open" : "";
		const status_label = meta.has_running ? __("Running") : meta.has_error ? __("Issue") : __("Done");
		const icon = meta.has_running ? "loader" : meta.has_error ? "x" : "check";

		return `
			<details class="bookkeeping-agent-workflow"${open_attr}>
				<summary class="bookkeeping-agent-workflow-summary">
					<span class="bookkeeping-agent-workflow-title">
						<span class="bookkeeping-agent-workflow-dot ${this.escape_html(meta.status_color || "green")}">
							${this.icon(icon, "xs")}
						</span>
						<span class="bookkeeping-agent-workflow-copy">
							<span class="bookkeeping-agent-workflow-eyebrow">${__("Bookkeeping run")}</span>
							<span class="bookkeeping-agent-workflow-line">${this.escape_html(meta.summary || __("Run details"))}</span>
						</span>
					</span>
					<span class="bookkeeping-agent-workflow-state ${this.escape_html(meta.status_color || "green")}">
						${this.escape_html(status_label)}
					</span>
				</summary>
				<div class="bookkeeping-agent-timeline">
					${events
						.map((event, index) => this.render_tool_timeline_item(event, index === events.length - 1))
						.join("")}
				</div>
			</details>
		`;
	}

	render_tool_timeline_item(event, is_last) {
		const status_class = this.get_status_class(event.status);
		const status_color = this.get_status_color(event.status, event.pending_action);
		const args = this.render_tool_arguments(event.arguments);
		const details = args
			? `
				<details class="bookkeeping-agent-tool-details">
					<summary>${__("Inputs")}</summary>
					<div class="bookkeeping-agent-tool-args">${args}</div>
				</details>
			`
			: "";

		return `
			<div class="bookkeeping-agent-tool-call bookkeeping-agent-tool-${status_class} ${is_last ? "is-last" : ""}">
				<div class="bookkeeping-agent-tool-status-dot ${status_color}">
					${this.get_tool_status_icon(event.status, event.pending_action)}
				</div>
				<div class="bookkeeping-agent-tool-main">
					<div class="bookkeeping-agent-tool-title">
						<span class="bookkeeping-agent-tool-name">
							${this.icon(this.get_tool_icon(event.tool), "xs")}
							<span>${this.escape_html(event.label)}</span>
						</span>
						<span class="bookkeeping-agent-tool-status ${status_color}">${this.escape_html(
							this.get_status_label(event.status, event.pending_action)
						)}</span>
					</div>
					${
						event.message
							? `<div class="bookkeeping-agent-tool-message">${this.escape_html(event.message)}</div>`
							: ""
					}
					${details}
				</div>
			</div>
		`;
	}

	get_run_summary(tool_count, has_running, has_error) {
		if (has_error) {
			return __("Run details need attention");
		}
		if (has_running) {
			return __("Working through the request");
		}
		if (tool_count === 1) {
			return __("1 tool call");
		}
		if (tool_count > 1) {
			return __("{0} tool calls", [tool_count]);
		}
		return __("Run details");
	}

	get_tool_status_icon(status, pending_action) {
		if (pending_action || status === "pending_confirmation" || status === "needs_input") {
			return this.icon("pause", "xs");
		}
		if (status === "error") {
			return this.icon("x", "xs");
		}
		if (status === "running") {
			return this.icon("loader", "xs");
		}
		return this.icon("check", "xs");
	}

	ensure_message_workflow($message) {
		let $workflow = $message.find(".bookkeeping-agent-message-workflow");
		if (!$workflow.length) {
			$workflow = $(`
				<div class="bookkeeping-agent-message-workflow">
					<div data-run-details></div>
					<div data-pending-actions></div>
				</div>
			`);
			$message.find(".bookkeeping-agent-message-content").append($workflow);
		}
		return $workflow;
	}

	get_pending_actions_container() {
		const $target =
			this.pending_actions_target ||
			this.current_assistant_message ||
			this.latest_assistant_message;

		if (!$target || !$target.length) {
			return null;
		}

		return this.ensure_message_workflow($target).find("[data-pending-actions]");
	}

	render_tool_arguments(args) {
		if (!args || !Object.keys(args).length) {
			return "";
		}

		return Object.entries(args)
			.map(([key, value]) => {
				return `
					<span class="bookkeeping-agent-tool-arg">
						<span class="bookkeeping-agent-tool-arg-key">${this.escape_html(key)}</span>
						<span>${this.escape_html(this.format_arg_value(value))}</span>
					</span>
				`;
			})
			.join("");
	}

	format_arg_value(value) {
		if (value === null || value === undefined) {
			return "";
		}
		if (typeof value === "object") {
			return JSON.stringify(value);
		}
		return String(value);
	}

	get_status_class(status) {
		return String(status || "ok")
			.toLowerCase()
			.replace(/[^a-z0-9_-]/g, "-");
	}

	get_status_color(status, pending_action) {
		if (pending_action || status === "pending_confirmation" || status === "needs_input") {
			return "orange";
		}
		if (status === "error") {
			return "red";
		}
		if (status === "running") {
			return "blue";
		}
		return "green";
	}

	get_status_label(status, pending_action) {
		if (pending_action || status === "pending_confirmation") {
			return __("Needs approval");
		}

		const labels = {
			running: __("Running"),
			ok: __("Done"),
			completed: __("Done"),
			needs_input: __("Needs input"),
			error: __("Error"),
		};
		return labels[status] || String(status || __("Done")).replace(/_/g, " ");
	}

	get_tool_icon(tool) {
		const icons = {
			search_records: "search",
			get_record: "file-search",
			run_bookkeeping_report: "chart-line",
			upsert_party: "contact",
			update_draft_document: "file-pen",
			create_sales_invoice_draft: "receipt",
			create_purchase_invoice_draft: "clipboard-plus",
			create_payment_entry_draft: "wallet",
			create_journal_entry_draft: "book-marked",
			submit_or_cancel_document: "check-circle",
		};
		return icons[tool] || "activity";
	}

	render_pending_actions() {
		let $container = this.get_pending_actions_container();

		if (!this.pending_actions.length) {
			if ($container && $container.length) {
				$container.empty();
			}
			this.pending_actions_target = null;
			this.update_composer_state();
			return;
		}

		if (!$container || !$container.length) {
			this.pending_actions_target = this.latest_assistant_message;
			$container = this.get_pending_actions_container();
		}

		if (!$container || !$container.length) {
			this.update_composer_state();
			return;
		}

		$container.html(`
			${this.render_approval_panel(this.pending_actions)}
		`);
		this.update_composer_state();
		this.scroll_messages_to_bottom();
	}

	render_approval_panel(actions) {
		return `
			<div class="bookkeeping-agent-approval-panel">
				<div class="bookkeeping-agent-approval-head">
					<span class="bookkeeping-agent-approval-icon">${this.icon("shield-check", "sm")}</span>
					<div>
						<div class="bookkeeping-agent-approval-kicker">${__("Approval required")}</div>
						<div class="bookkeeping-agent-approval-title">${__("Review before ERPNext changes data")}</div>
					</div>
				</div>
				${actions.map((action, index) => this.render_pending_action(action, index)).join("")}
			</div>
		`;
	}

	render_pending_action(action, index) {
		const title = this.get_action_title(action);
		const safety_note = this.get_action_safety_note(action);
		return `
			<div class="bookkeeping-agent-action">
				<div class="bookkeeping-agent-action-main">
					<div>
						<div class="bookkeeping-agent-action-title">${this.escape_html(title)}</div>
						<div class="bookkeeping-agent-action-safety">${this.escape_html(safety_note)}</div>
						<div class="bookkeeping-agent-action-impact">${this.escape_html(action.impact)}</div>
					</div>
				</div>
				<details class="bookkeeping-agent-action-details">
					<summary>${__("Review fields")}</summary>
					<pre>${this.escape_html(JSON.stringify(action.args || {}, null, 2))}</pre>
				</details>
				<div class="bookkeeping-agent-action-buttons">
					<button class="btn btn-sm btn-primary" type="button" data-confirm-action="${index}" ${this.is_loading ? "disabled" : ""}>
						${this.icon("check", "xs")}
						<span>${__("Confirm")}</span>
					</button>
					<button class="btn btn-sm btn-default" type="button" data-cancel-action="${index}" ${this.is_loading ? "disabled" : ""}>
						${this.icon("x", "xs")}
						<span>${__("Cancel")}</span>
					</button>
				</div>
			</div>
		`;
	}

	get_action_title(action) {
		const summary = action?.summary || __("Run ERPNext action");
		return summary.endsWith("?") ? summary : `${summary}?`;
	}

	get_action_safety_note(action) {
		const tool = action?.tool || "";
		if (tool === "submit_or_cancel_document") {
			return __("This can affect ledger entries.");
		}
		if (tool.startsWith("create_") && tool.endsWith("_draft")) {
			return __("Draft only. No ledger posting.");
		}
		if (tool === "update_draft_document") {
			return __("Draft document only. Submitted documents stay locked.");
		}
		return __("Runs only after you confirm.");
	}

	render_result_link(summary) {
		if (!summary || !summary.url) {
			return;
		}

		const label = `${summary.doctype || __("Document")} ${summary.name || ""}`.trim();
		const $link = this.render_conversation_message("assistant", { kind: "result" });
		$link.find(".bookkeeping-agent-avatar").html(this.icon("external-link", "sm"));
		$link.find(".bookkeeping-agent-message-body").html(`
			<a class="bookkeeping-agent-result-link" href="${this.escape_html(
				summary.url
			)}" target="_blank" rel="noopener noreferrer">
				${this.icon("external-link", "xs")}
				<span>${this.escape_html(label)}</span>
			</a>
		`);
		this.$messages.append($link);
		this.scroll_messages_to_bottom();
	}

	set_loading(is_loading) {
		this.is_loading = is_loading;
		this.update_composer_state();
		this.$status
			.removeClass("gray orange green")
			.addClass(is_loading ? "orange" : "gray")
			.text(is_loading ? __("Working") : __("Ready"));
	}

	is_composer_disabled() {
		return this.is_loading || this.pending_actions.length > 0;
	}

	update_composer_state() {
		const has_pending_actions = this.pending_actions.length > 0;
		const disabled = this.is_loading || has_pending_actions;
		const placeholder = has_pending_actions
			? __("Review the pending action to continue")
			: __("Ask about invoices, payments, reports, or draft actions");
		const hint = has_pending_actions
			? __("Approve or cancel the pending ERPNext action before sending another message.")
			: __("Enter to send. Shift + Enter for a new line.");

		this.$input.prop("disabled", disabled).attr("placeholder", placeholder);
		this.$send
			.prop("disabled", disabled)
			.toggleClass("is-loading", Boolean(this.is_loading))
			.html(this.icon(this.is_loading ? "loader" : "send-horizontal", "sm"));
		this.$composer.toggleClass("is-disabled", disabled);
		this.$composer_hint.text(hint);
	}

	resize_input() {
		const input = this.$input[0];
		if (!input) {
			return;
		}

		this.$input.css("height", "40px");
		if (!this.$input.val()) {
			return;
		}

		this.$input.css("height", "auto");
		this.$input.css("height", `${Math.min(input.scrollHeight, 144)}px`);
	}

	scroll_messages_to_bottom() {
		const messages = this.$messages[0];
		if (messages) {
			messages.scrollTop = messages.scrollHeight;
		}
	}

	get_error_message(error) {
		const fallback = __("The request could not be completed.");
		const server_messages =
			error?._server_messages || error?.responseJSON?._server_messages || error?.responseText;
		const parsed_message = this.parse_server_message(server_messages);
		const message = parsed_message || error?.message || fallback;
		return this.sanitize_error_message(message);
	}

	parse_server_message(server_messages) {
		if (!server_messages) {
			return "";
		}

		try {
			const messages = typeof server_messages === "string" ? JSON.parse(server_messages) : server_messages;
			if (!Array.isArray(messages) || !messages.length) {
				return "";
			}

			const first = typeof messages[0] === "string" ? JSON.parse(messages[0]) : messages[0];
			return first.message || first.title || "";
		} catch (error) {
			return "";
		}
	}

	sanitize_error_message(message) {
		const text = $("<div>").html(message || "").text().trim();
		return (
			text
				.replace(/sk-[A-Za-z0-9_-]+/g, "sk-...")
				.replace(/(api[_ -]?key\\s*[:=]\\s*)[^\\s,;]+/gi, "$1***")
				.slice(0, 500) || __("The request could not be completed.")
		);
	}

	render_plain_text(text) {
		return this.escape_html(text || "").replace(/\n/g, "<br>");
	}

	render_markdown(text) {
		const source = this.escape_raw_html(text || "");
		let html = "";
		if (frappe.markdown) {
			html = frappe.markdown(source);
		} else {
			html = this.render_plain_text(source);
		}
		return this.sanitize_markdown_html(html);
	}

	escape_raw_html(value) {
		return String(value || "")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;");
	}

	sanitize_markdown_html(html) {
		const template = document.createElement("template");
		template.innerHTML = frappe.dom?.remove_script_and_style
			? frappe.dom.remove_script_and_style(html || "")
			: html || "";

		const allowed_tags = new Set([
			"a",
			"blockquote",
			"br",
			"code",
			"del",
			"em",
			"h1",
			"h2",
			"h3",
			"h4",
			"h5",
			"h6",
			"hr",
			"li",
			"ol",
			"p",
			"pre",
			"strong",
			"table",
			"tbody",
			"td",
			"th",
			"thead",
			"tr",
			"ul",
		]);

		Array.from(template.content.querySelectorAll("*")).forEach((element) => {
			const tag = element.tagName.toLowerCase();
			if (!allowed_tags.has(tag)) {
				element.replaceWith(...Array.from(element.childNodes));
				return;
			}

			Array.from(element.attributes).forEach((attribute) => {
				const name = attribute.name.toLowerCase();
				if (name.startsWith("on") || name === "style" || name === "class") {
					element.removeAttribute(attribute.name);
					return;
				}

				if (tag === "a" && name === "href") {
					const safe_href = this.get_safe_href(attribute.value);
					if (!safe_href) {
						element.removeAttribute("href");
						return;
					}
					element.setAttribute("href", safe_href);
					return;
				}

				if (tag === "a" && name === "title") {
					return;
				}

				element.removeAttribute(attribute.name);
			});

			if (tag === "a") {
				const href = element.getAttribute("href");
				element.removeAttribute("target");
				element.removeAttribute("rel");
				if (href && this.should_open_href_in_new_tab(href)) {
					element.setAttribute("target", "_blank");
					element.setAttribute("rel", "noopener noreferrer");
				}
			}
		});

		return this.linkify_desk_routes(template.innerHTML);
	}

	linkify_desk_routes(html) {
		return String(html || "")
			.split(/(<a\b[\s\S]*?<\/a>)/gi)
			.map((segment) => {
				if (/^<a\b/i.test(segment)) {
					return segment;
				}
				return this.linkify_desk_routes_in_segment(segment);
			})
			.join("");
	}

	linkify_desk_routes_in_segment(segment) {
		return String(segment || "").replace(/(^|[\s([{:>])((?:\/app\/)[^\s<>"']+)/g, (match, prefix, raw_href) => {
			const { href, suffix } = this.split_trailing_link_punctuation(raw_href);
			if (!this.is_desk_href(href)) {
				return match;
			}

			const safe_href = this.escape_html(href);
			return `${prefix}<a href="${safe_href}" target="_blank" rel="noopener noreferrer">${safe_href}</a>${suffix}`;
		});
	}

	split_trailing_link_punctuation(raw_href) {
		let href = String(raw_href || "");
		let suffix = "";
		while (/[.,;:!?)]$/.test(href)) {
			suffix = `${href.slice(-1)}${suffix}`;
			href = href.slice(0, -1);
		}
		return { href, suffix };
	}

	get_safe_href(href) {
		const value = String(href || "").trim();
		const compact = value.replace(/[\u0000-\u001F\u007F\s]+/g, "").toLowerCase();
		if (!value || compact.startsWith("javascript:") || compact.startsWith("data:") || compact.startsWith("vbscript:")) {
			return "";
		}

		if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {
			try {
				const parsed = new URL(value, window.location.origin);
				return ["http:", "https:", "mailto:"].includes(parsed.protocol) ? value : "";
			} catch (error) {
				return "";
			}
		}

		return value;
	}

	should_open_href_in_new_tab(href) {
		return this.is_desk_href(href) || this.is_external_href(href);
	}

	is_desk_href(href) {
		try {
			const parsed = new URL(href, window.location.origin);
			return parsed.origin === window.location.origin && parsed.pathname.startsWith("/app/");
		} catch (error) {
			return false;
		}
	}

	is_external_href(href) {
		try {
			const parsed = new URL(href, window.location.origin);
			return parsed.origin !== window.location.origin && ["http:", "https:"].includes(parsed.protocol);
		} catch (error) {
			return false;
		}
	}

	escape_html(value) {
		if (frappe.utils && frappe.utils.escape_html) {
			return frappe.utils.escape_html(value || "");
		}
		return $("<div>").text(value || "").html();
	}

	icon(name, size = "sm") {
		return frappe.utils && frappe.utils.icon ? frappe.utils.icon(name, size) : "";
	}

	clear_empty_state() {
		if (this.$empty_state && this.$empty_state.length) {
			this.$empty_state.remove();
			this.$empty_state = null;
		}
	}
};

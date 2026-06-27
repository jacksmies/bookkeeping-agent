(function () {
	const legacyDeskPath = "/desk/bookkeeping-agent";
	const agentPath = "/app/bookkeeping-agent";

	function redirectLegacyDeskRoute() {
		if (window.location.pathname.replace(/\/$/, "") === legacyDeskPath) {
			window.location.replace(agentPath);
		}
	}

	redirectLegacyDeskRoute();
	window.addEventListener("hashchange", redirectLegacyDeskRoute);
	window.addEventListener("popstate", redirectLegacyDeskRoute);
})();

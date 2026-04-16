// Copyright (c) 2026, Sakthi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Crawled Grant Record", {
	visit_source(frm) {
		if (frm.doc.source_url) {
			window.open(frm.doc.source_url, "_blank");
		} else {
			frappe.msgprint(__("No Source URL available for this record."));
		}
	},
});

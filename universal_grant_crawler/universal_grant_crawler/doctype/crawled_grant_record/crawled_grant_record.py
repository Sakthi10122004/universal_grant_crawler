# Copyright (c) 2026, Sakthi and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from datetime import datetime, date


class CrawledGrantRecord(Document):
	def before_save(self):
		"""Auto-compute status based on the deadline field."""
		self.status = self._compute_status()

	def _compute_status(self):
		"""Parse the deadline string and return 'Active' or 'Expired'."""
		deadline_str = (self.deadline or "").strip()

		# Deadlines that are inherently open-ended → always Active
		if not deadline_str or deadline_str.lower() in ("not specified", "rolling", "open", "ongoing", "tbd"):
			return "Active"

		# Try common date formats (the LLM is instructed to use YYYY-MM-DD)
		for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
			try:
				deadline_date = datetime.strptime(deadline_str, fmt).date()
				return "Expired" if deadline_date < date.today() else "Active"
			except ValueError:
				continue

		# Couldn't parse → assume Active (better to keep than wrongly delete)
		return "Active"

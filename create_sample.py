"""Run this once to generate a sample Excel template: python create_sample.py"""
import openpyxl

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Accounts"

# Headers
ws.append(["ID", "Password", "TOTP Secret", "Status"])

# Sample rows
ws.append(["user@example.com", "Pass@1234", "JBSWY3DPEHPK3PXP", "available"])
ws.append(["admin@example.com", "Admin@5678", "JBSWY3DPEHPK3PXQ", "available"])
ws.append(["test@example.com", "Test@9999", "JBSWY3DPEHPK3PXR", "available"])

# Style headers
from openpyxl.styles import Font, PatternFill
for cell in ws[1]:
    cell.font = Font(bold=True)
    cell.fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    cell.font = Font(bold=True, color="FFFFFF")

# Column widths
ws.column_dimensions["A"].width = 30
ws.column_dimensions["B"].width = 20
ws.column_dimensions["C"].width = 35
ws.column_dimensions["D"].width = 15

wb.save("sample_accounts.xlsx")
print("✅ sample_accounts.xlsx created!")

import os
import requests
from markitdown import MarkItDown
import sys

def parse_oag_report(pdf_url, output_md):
    print(f"Downloading OAG report from {pdf_url}...")
    response = requests.get(pdf_url)
    response.raise_for_status()
    
    temp_pdf = "oag_report_temp.pdf"
    with open(temp_pdf, "wb") as f:
        f.write(response.content)
    
    print("Converting to Markdown using Likhit...")
    md = MarkItDown(enable_plugins=True)
    result = md.convert(temp_pdf)
    
    with open(output_md, "w", encoding="utf-8") as f:
        f.write(result.text_content)
    
    print(f"Successfully saved Markdown to {output_md}")
    os.remove(temp_pdf)

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://oag.gov.np/downloadfile/Annual-Report-61.pdf"
    output = sys.argv[2] if len(sys.argv) > 2 else "OAG_Report_61.md"
    parse_oag_report(url, output)

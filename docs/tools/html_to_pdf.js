// Print an HTML file to PDF with headless Chromium.
// Usage: node html_to_pdf.js <input.html> <output.pdf>
// Needs puppeteer on the module path (it ships with @mermaid-js/mermaid-cli, which also renders the diagrams).
const path = require("path");
const puppeteer = require("puppeteer");

(async () => {
  const [input, output] = process.argv.slice(2);
  const browser = await puppeteer.launch();
  const page = await browser.newPage();
  await page.goto("file:///" + path.resolve(input).replace(/\\/g, "/"), { waitUntil: "networkidle0" });
  await page.pdf({
    path: output,
    format: "A4",
    printBackground: true,
    preferCSSPageSize: true,
    displayHeaderFooter: true,
    headerTemplate: "<span></span>",
    footerTemplate:
      '<div style="font-size:8px;color:#777;width:100%;text-align:center;font-family:Segoe UI,Arial">' +
      'Website redesign & business listings — design document · page <span class="pageNumber"></span> of <span class="totalPages"></span></div>',
    margin: { top: "16mm", bottom: "18mm", left: "15mm", right: "15mm" },
  });
  await browser.close();
  console.log("wrote", output);
})();

const puppeteer = require("puppeteer");

async function generateWithPuppeteer({ html, footerText }) {
  const browser = await puppeteer.launch();
  try {
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: "networkidle0" });

    const pdfBuffer = await page.pdf({
      format: "A4",
      printBackground: true,
      displayHeaderFooter: Boolean(footerText),
      headerTemplate: "<div></div>",
      footerTemplate: footerText
        ? `<div style=\"font-size:9px;width:100%;text-align:center;color:#666;padding:0 10px;\">${footerText}</div>`
        : "<div></div>",
      margin: { top: "20mm", right: "15mm", bottom: "20mm", left: "15mm" }
    });

    return pdfBuffer;
  } finally {
    await browser.close();
  }
}

module.exports = { generateWithPuppeteer };

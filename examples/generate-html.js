const fs = require("fs");
const path = require("path");
const { generateWithPuppeteer } = require("../src/generateWithPuppeteer");

function fillTemplate(template, data) {
  return template
    .replaceAll("{{date}}", data.date)
    .replaceAll("{{user}}", data.user)
    .replaceAll("{{total}}", data.total);
}

(async () => {
  const outPath = path.join(__dirname, "out-html.pdf");
  const templatePath = path.join(__dirname, "template.html");

  const template = fs.readFileSync(templatePath, "utf8");
  const html = fillTemplate(template, {
    date: new Date().toISOString(),
    user: "Luis",
    total: "$123.45"
  });

  const pdfBuffer = await generateWithPuppeteer({
    html,
    footerText: "<span class=\"pageNumber\"></span>/<span class=\"totalPages\"></span>"
  });

  fs.writeFileSync(outPath, pdfBuffer);
  process.stdout.write(`Generado: ${outPath}\n`);
})();

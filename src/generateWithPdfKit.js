const PDFDocument = require("pdfkit");

function generateWithPdfKit({ title, lines }) {
  const doc = new PDFDocument({ size: "A4", margin: 40 });

  const chunks = [];
  doc.on("data", (c) => chunks.push(c));

  return new Promise((resolve, reject) => {
    doc.on("end", () => resolve(Buffer.concat(chunks)));
    doc.on("error", reject);

    doc.font("Helvetica-Bold").fontSize(18).text(title);
    doc.moveDown(0.75);

    doc.font("Helvetica").fontSize(12);
    for (const line of lines) {
      doc.text(String(line), { width: 515 });
    }

    doc.end();
  });
}

module.exports = { generateWithPdfKit };

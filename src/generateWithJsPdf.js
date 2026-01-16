const { jsPDF } = require("jspdf");

function generateWithJsPdf({ title, lines }) {
  const doc = new jsPDF({ unit: "pt", format: "a4" });

  doc.setFont("helvetica", "bold");
  doc.setFontSize(18);
  doc.text(title, 40, 60);

  doc.setFont("helvetica", "normal");
  doc.setFontSize(12);

  let y = 95;
  for (const line of lines) {
    doc.text(String(line), 40, y);
    y += 18;
    if (y > 800) {
      doc.addPage();
      y = 60;
    }
  }

  const arrayBuffer = doc.output("arraybuffer");
  return Buffer.from(arrayBuffer);
}

module.exports = { generateWithJsPdf };

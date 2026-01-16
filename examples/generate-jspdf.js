const fs = require("fs");
const path = require("path");
const { generateWithJsPdf } = require("../src/generateWithJsPdf");

(async () => {
  const outPath = path.join(__dirname, "out-jspdf.pdf");
  const buffer = generateWithJsPdf({
    title: "PDF con jsPDF",
    lines: [
      "Hola Luis,",
      "Este PDF fue generado con jsPDF.",
      "- Texto simple",
      "- Salto de página automático"
    ]
  });

  fs.writeFileSync(outPath, buffer);
  process.stdout.write(`Generado: ${outPath}\n`);
})();

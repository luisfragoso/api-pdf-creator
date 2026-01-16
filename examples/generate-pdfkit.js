const fs = require("fs");
const path = require("path");
const { generateWithPdfKit } = require("../src/generateWithPdfKit");

(async () => {
  const outPath = path.join(__dirname, "out-pdfkit.pdf");
  const buffer = await generateWithPdfKit({
    title: "PDF con PDFKit",
    lines: [
      "Hola Luis,",
      "Este PDF fue generado con PDFKit.",
      "- Control fino de layout",
      "- Streams / buffers"
    ]
  });

  fs.writeFileSync(outPath, buffer);
  process.stdout.write(`Generado: ${outPath}\n`);
})();

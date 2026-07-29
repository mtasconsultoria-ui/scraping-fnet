import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Consulta de Fundos — FundosNET / CVM",
  description:
    "Busca de fundos por características e por conteúdo dos regulamentos (FundosNET e Dados Abertos CVM)",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-BR">
      <body>{children}</body>
    </html>
  );
}

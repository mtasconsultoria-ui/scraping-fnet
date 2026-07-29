/** @type {import('next').NextConfig} */
const nextConfig = {
  // `pg` é nativo do servidor; não deve ser empacotado para o bundle do cliente
  serverExternalPackages: ["pg"],
};

export default nextConfig;

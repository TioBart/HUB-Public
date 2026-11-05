// Configuração do login (Supabase). Deixe em branco para usar o site sem login:
// as preferências continuam funcionando, salvas só no navegador.
//
// Onde pegar os valores: no painel do Supabase, Project Settings > API.
// A "anon key" é pública por natureza; quem protege os dados é a regra de
// segurança criada pelo arquivo supabase.sql. Nunca coloque aqui a "service_role key".
window.HUB_CONFIG = {
  supabaseUrl: '',      // exemplo: 'https://abcdefgh.supabase.co'
  supabaseAnonKey: ''   // exemplo: 'eyJhbGciOi...'
};

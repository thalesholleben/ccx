# Security

## Modelo de ameaça

O `ccx` le e escreve tokens OAuth reais (Claude Code e Codex CLI) em arquivos
locais. Pontos que importam para quem for auditar ou confiar no codigo:

- **Nenhuma rede alem do provedor oficial.** `ccx.py` fala so com
  `api.anthropic.com` (leitura de cota) e `platform.claude.com` (refresh de
  token). `ccx_codex.py` fala so com `chatgpt.com` (leitura de cota) e
  `auth.openai.com` (refresh de token). Sem telemetria, sem terceiros.
- **Sem ping de aquecimento.** A leitura de cota nunca manda prompt nem abre
  janela de uso. Ver a secao "Termos de uso" do `README.md`.
- **Estado local fica em `~/.ccx/accounts.json` e `~/.ccx/codex_accounts.json`**,
  contendo os tokens OAuth de cada conta cadastrada. Esse diretorio nunca deve
  ser versionado, copiado para fora da maquina ou compartilhado.
- **Trocas de credencial sao cirurgicas e atomicas**: reescrevem so os campos
  de identidade, via arquivo temporario + `os.replace`, para nunca deixar um
  `.credentials.json`/`auth.json` truncado no meio de uma escrita.
- **Sem lock cooperativo confirmado com o Codex CLI real** (diferente do
  modulo Claude, que segura o lock de diretorio documentado no proprio
  codigo do Claude Code). Ver a secao "Modulo Codex" do `README.md` para o
  detalhe dessa janela de corrida conhecida.
- **Hot-swap global nao isola sessoes persistentes.** O Codex pode manter a
  autenticacao em memoria depois de ler `auth.json`; trocar esse arquivo nao
  migra com seguranca um processo ja aberto. Para agentes simultaneos, use
  perfis separados por processo (`CODEX_HOME` / `CLAUDE_CONFIG_DIR`) ou uma
  camada de proxy com afinidade de sessao. `ccx_profile.py` cria esses perfis
  apenas para o processo filho e não toca nas credenciais globais existentes.
- **Logs de monitor não carregam o texto da exceção.** Falhas inesperadas durante
  inicialização ou rotação registram apenas a classe, porque mensagens de rede ou
  de autenticação podem conter dados sensíveis.
- **Leituras de cota sao amortizadas entre processos.** `status`, hooks e
  monitores compartilham um cache local curto (30 s para sucesso e 120 s para
  erro). Isso reduz rajadas contra os endpoints oficiais sem atrasar o poll do
  monitor. Um `HTTP 429` sem historico nao e interpretado como conta esgotada;
  uma leitura recente que ja confirmou o esgotamento continua valida por 5 min
  apenas para decidir uma troca segura.

## Reportando uma vulnerabilidade

Abra uma [GitHub Security Advisory](../../security/advisories/new) neste
repositorio, ou um issue normal se nao for algo sensivel. Sem SLA formal:
projeto pessoal mantido no tempo livre, mas relatos sao lidos e respondidos.

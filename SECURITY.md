# Security

## Modelo de ameaça

O `ccx` le e escreve tokens OAuth reais (Claude Code e Codex CLI) em arquivos
locais. Pontos que importam para quem for auditar ou confiar no codigo:

- **Nenhuma rede externa alem do provedor oficial.** `ccx.py` fala so com
  `api.anthropic.com` (leitura de cota) e `platform.claude.com` (refresh de
  token). `ccx_codex.py` fala so com `chatgpt.com` (leitura de cota) e
  `auth.openai.com` (refresh de token). O bridge opcional abre apenas uma porta
  aleatoria em `127.0.0.1`. Sem telemetria, sem terceiros.
- **Sem ping de aquecimento.** A leitura de cota nunca manda prompt nem abre
  janela de uso. Ver a secao "Termos de uso" do `README.md`.
- **Estado local fica em `~/.ccx/accounts.json` e `~/.ccx/codex_accounts.json`**,
  contendo os tokens OAuth de cada conta cadastrada. Esse diretorio nunca deve
  ser versionado, copiado para fora da maquina ou compartilhado.
- **Trocas de credencial sao cirurgicas e atomicas**: reescrevem so os campos
  de identidade, via arquivo temporario + `os.replace`, para nunca deixar um
  `.credentials.json`/`auth.json` truncado no meio de uma escrita.
- **Sem o bridge, nao ha lock cooperativo confirmado com o Codex CLI real** (diferente do
  modulo Claude, que segura o lock de diretorio documentado no proprio
  codigo do Claude Code). Ver a secao "Modulo Codex" do `README.md` para o
  detalhe dessa janela de corrida conhecida.
- **Hot-swap nao e isolamento entre agentes.** O bridge opt-in migra exatamente
  um app-server depois de todos os turnos ativos terminarem. Mais de um bridge
  no mesmo `CODEX_HOME` bloqueia a operacao inteira. Para agentes simultaneos,
  use perfis separados por processo (`CODEX_HOME` / `CLAUDE_CONFIG_DIR`).
  `ccx_profile.py` cria esses perfis apenas para o processo filho e não toca nas
  credenciais globais existentes.
- **Controle local autenticado.** O registro em `~/.ccx/codex_bridges/` contem
  PID, marca de processo, porta e segredo aleatorio de 256 bits, nunca token. O
  access token passa apenas pelo socket loopback autenticado; refresh/id token
  permanecem no store. Frames tem limite de 64 KiB e nunca sao registrados.
- **Troca Codex e transacional.** O app-server confirma a conta alvo antes da
  escrita de `auth.json`; novos trabalhos ficam retidos ate o commit. Se a
  escrita falhar, o bridge restaura a conta anterior antes de liberar a fila.
  O callback de refresh rejeita `previousAccountId` diferente da conta externa
  atual para nao misturar identidades dentro do retry de um turno.
- **Backup local do VS Code.** Enquanto o bridge esta instalado, o instalador
  preserva o `settings.json` anterior em
  `settings.json.ccx-codex-bridge.bak`, no mesmo diretorio e sob a mesma fronteira
  de acesso do perfil. Esse arquivo pode repetir segredos que o usuario ja tinha
  nas settings: nunca e lido pelo bridge, transmitido ou versionado, e so e
  removido depois de um uninstall bem-sucedido.
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

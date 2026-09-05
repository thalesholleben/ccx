# AGENTS.md

Mapa rapido para quem (humano ou agente) for mexer neste repositorio.

## O que e

Dois modulos Python stdlib monitoram a cota de cada provedor e trocam de conta
antes de bater o limite:

- `ccx.py` - contas Claude Code (le `~/.claude/.credentials.json` e `~/.claude.json`)
- `ccx_codex.py` - contas Codex CLI / ChatGPT (le `~/.codex/auth.json`), importa
  `ccx.py` e reusa de la a engine de decisao e os primitivos de IO/lock
- `ccx_codex_bridge.py` - bridge opt-in entre a extensao e o app-server, para
  aplicar uma troca Codex no processo vivo depois do turno corrente

Sem framework nem dependencia externa. O modo padrao so le cota e troca a
credencial cirurgicamente; o bridge opcional acrescenta um controle autenticado
em loopback, sem expor um proxy de chamadas ao modelo.

## Comandos de validacao

```bash
python test_ccx.py                # 59 testes, decisao + IO do modulo Claude
python test_ccx_codex.py          # 39 testes, especifico do modulo Codex
python test_ccx_profile.py        # 4 testes, perfis isolados
python test_ccx_codex_bridge.py   # 18 testes, bridge/launcher/instalador/runtime
```

Sem framework de teste, so `assert` e um runner minimo no final de cada
arquivo. Rodar os quatro antes de qualquer PR.

## Regras ao editar

- Qualquer troca de credencial tem que continuar **cirurgica**: reescrever so
  os campos que identificam a conta, nunca o arquivo inteiro. O
  `.credentials.json` do Claude Code guarda `mcpOAuth` (tokens de servidores
  MCP) que uma escrita completa derrubaria.
- Identidade de conta ativa casa por **email/organizacao**, nunca por token.
  Os dois provedores rotacionam o refresh token da conta em uso; comparar por
  token faz a identidade se perder na primeira rotacao.
- Toda escrita em disco e via arquivo temporario + `os.replace` (atomica).
  JSON corrompido levanta `CorruptFile`, nunca vira `{}` silenciosamente (um
  `{}` faria o passo seguinte reescrever o arquivo e apagar o que nao foi
  lido).
- `ccx_codex.py` importa `ccx.py` para reusar a engine de decisao
  (`pick_target`, `band_delay`, `next_wake`) e os primitivos de lock/IO. Nao
  duplicar essas funcoes; se algo generico precisar mudar, muda em `ccx.py` e
  o modulo Codex herda.
- `status`, `hook` e `auto` compartilham `usage_cache`. Qualquer coleta deve
  adquirir o lock e reler o store antes de decidir o que consultar: processos
  concorrentes podem ter preenchido o cache enquanto este processo esperava.
- Consulte periodicamente somente o slot ativo. A cota de um slot inativo nao
  cresce, portanto seu ultimo snapshot continua valido e resets vencidos podem
  ser projetados localmente para 0%. Ao trocar, preserve esse snapshot; ele e o
  fallback se a primeira leitura da nova ativa receber 429.
- O snapshot inativo so mascara a primeira falha da nova ativa quando ela for
  exatamente `HTTP 429`. Um 401, timeout ou erro de formato volta como desconhecido.
- `HTTP 429` na leitura de uso e falha de medicao, nao prova de cota esgotada.
  Sem leitura conhecida, manter a ativa. Se uma leitura recente ja a confirmou
  esgotada, ela pode orientar a troca mesmo que a releitura tenha dado 429.
- `fetch_usage` deve preservar o maior limite `weekly_scoped` por modelo como
  janela `modelo`. A decisao nao sabe qual modelo uma sessao persistente vai
  pedir: ignorar essa janela repete o falso positivo de "tem cota" quando o
  modelo em uso ja recusou a conta.
- Se dois limites `weekly_scoped` empatam em percentual, preserve o reset mais
  tarde. A ordem do payload nao pode anunciar disponibilidade cedo demais.
- `do_switch` deve reler o store depois de adquirir o lock. A coleta solta o
  lock antes da decisao, e gravar o snapshot antigo pode ressuscitar refresh
  token ou apagar cache que outro hook acabou de salvar.
- Limiar e cooldown do Claude sao calibrados **juntos**. A folga que o limiar compra,
  `(100 - threshold) / burn_rate`, tem que ser maior que
  `cooldown + maior POLL_TIGHT`, senao o cooldown sobrevive a folga e o monitor
  fica preso numa conta que morreu dentro da trava. Mexer em um sem olhar o
  outro recria o travamento de 19/08/2026.
- O escape de cooldown (`cooldown_blocks`) dispara em util >= 100, nao no limiar.
  Escapar no limiar equivale a remover o cooldown, porque um alvo diferente da
  ativa ja implica que a ativa esta pior. Cota ilegivel nunca escapa.
- Somente quando todos os slots tiverem cota conhecida e estiverem em 100%,
  `pick_target` deve escolher
  a conta cujo `available_at` e mais cedo. Se duas janelas da mesma conta estiverem
  travadas, disponibilidade e o reset mais tarde entre elas. Nao aplique margem de
  empate: primeiro destrava quem realmente ficar disponivel primeiro; depois a
  selecao normal pode migrar para a conta de reset semanal mais proximo.
- No fallback do `consume-first`, utilizacoes com diferenca estritamente menor que
  5 pontos empatam e o reset semanal mais proximo vence. `best` continua escolhendo
  a menor utilizacao sem esse desempate.
- O predicado de cooldown mora em `ccx.py` e os dois `check_once` o chamam. Nao
  duplicar: era copia colada nos dois modulos e so um dos lados seria corrigido.
- Faixas de poll sao por provedor (`ccx.PollBands`). O modulo Codex passa as suas
  em `BANDS` porque la o rotulo "5h" e posicional e costuma carregar a janela
  semanal, que nao se move dentro de uma sessao; herdar a faixa apertada do
  Claude prenderia o poll no ritmo rapido por dias.
- Toda troca registra o snapshot da decisao no `auto.log` via o `reason` do
  `do_switch`. Percentual e numero de slot podem entrar; token, e-mail e payload
  nao.
- `pinned_slot` e uma trava de operador, nao uma preferencia. Quando existe,
  `check_once` sai antes de qualquer coleta: fixar uma conta nao pode gerar
  trafego de usage. E o `do_switch(..., only_if_pinned=True)` revalida a
  fixacao ja sob o lock, senao um `--pin off` concorrente seria desfeito por
  uma troca decidida antes dele.
- `pinned_slot` invalido (slot inexistente ou tipo errado) levanta erro em vez
  de virar `None`. Cair em rotacao silenciosa e pior do que parar: o operador
  fixou justamente para a conta nao mudar.
- Locks internos do CCX guardam PID, id e marca de criação do processo. PID vivo
  com marca divergente é reciclado e pode ser retomado; marca temporariamente
  ilegível ou lock legado sem marca conserva o lock enquanto o PID estiver vivo.
  `discard_lock(..., None)` só pode remover um diretório ainda vazio: nunca apague
  `owner.json` sem conferir a identidade do dono.
- Falha antes do loop de `ccx.py auto` só vai para `auto.log` como classe da exceção
  ou como configuração insuficiente; nunca registre `str(exc)`, token ou payload.
- Sem o bridge, a troca do arquivo global de autenticacao serve para uso
  sequencial e um Codex persistente nao migra. Com o bridge, `do_switch` deve
  confirmar `account/login/start(chatgptAuthTokens)` antes de gravar disco e
  manter a barreira fechada ate o commit; falha local faz rollback em memoria.
- O bridge rastreia turnos por `(threadId, turn.id)`, segura todos os metodos de
  trabalho listados em `WORK_METHODS` e nunca troca no meio de um turno. Respostas
  internas atrasadas continuam consumidas pelo ID exato e nunca vazam a extensao.
- Refresh externo so pode devolver token do `previousAccountId` atualmente ativo.
  Outro account ID falha rapido; trocar de slot durante retry de 401 misturaria
  identidades dentro do mesmo turno.
- Registro do bridge pode conter PID, marca de processo, porta e segredo, nunca
  token. O controle fica em `127.0.0.1`, autentica antes de aceitar operacao e
  limita frames. Nao logar JSON-RPC: turnos contem texto do usuario.
- Mais de um app-server no mesmo `CODEX_HOME` recusa a troca inteira. Hot-swap
  nao e isolamento entre agentes paralelos; para isso use perfis por processo.
- O launcher nativo precisa preservar argv/cwd/env/stdin/stdout/stderr/exit code,
  localizar o Codex atualizado no PATH e manter Python + app-server em Job Object
  com `KILL_ON_JOB_CLOSE`, para a extensao nao deixar filhos orfaos.
- O instalador do bridge e cirurgico e reversivel: recusa custom executable de
  terceiro/WSL, valida pacote e assinatura, encontra o objeto raiz sem confundir
  comentarios JSONC, preserva um backup ao lado do settings enquanto instalado,
  ignora o path no Settings Sync e nao reinicia nem encerra VS Code por conta propria.
- `ccx_profile.py` e o caminho opt-in para paralelismo: não copie tokens nem
  mude o ambiente do processo pai. Ele só cria o diretório do perfil e inicia
  um processo filho com `CLAUDE_CONFIG_DIR` ou `CODEX_HOME` próprio. Para
  Codex, preserve a flag `cli_auth_credentials_store="file"` antes dos args.
- Nunca commitar `~/.ccx/accounts.json` ou `~/.ccx/codex_accounts.json`
  (tokens OAuth reais). Ja estao no `.gitignore`.

## O que ignorar

- `__pycache__/`, `.pytest_cache/`

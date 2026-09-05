# Auditoria final — CCX

Data: 2026-09-05

Revisor independente: Claude Code, modelo canônico `claude-opus-5`, effort `high`

Modo: cross-review completo, sem edição de código pelo revisor

## Veredito

**FIX_CONFIRMADO** — o bloqueador P1 da primeira rodada foi reproduzido e corrigido.
Não restou bloqueador para publicar este lote.

## Escopo

O diff auditado reúne as pendências operacionais do CCX e o hot-swap do Codex:

- monitor Claude com limiar padrão de 80% e cooldown de 60 s;
- leitura remota somente da conta ativa, mantendo snapshots das inativas e
  projetando resets localmente;
- restrição semanal por modelo e desempate conservador;
- troca imediata para uma conta disponível, sem esperar uma opção futura melhor;
- quando todas estão bloqueadas, estacionamento na conta que libera primeiro;
- fixação e liberação de slot;
- tratamento seguro de 429/401 e compatibilidade com caches legados;
- bridge opt-in para trocar a conta do app-server Codex vivo depois do turno atual;
- launcher nativo, instalador Windows, documentação e testes correspondentes.

O protocolo do hot-swap foi confrontado com a
[documentação oficial do Codex app-server](https://developers.openai.com/codex/app-server/).
O fluxo usa `account/login/start` com `chatgptAuthTokens` e capability
`experimentalApi`.

## Arquitetura validada

1. A extensão inicia `ccx-codex-bridge.exe`, que preserva argv, ambiente, cwd,
   stdio e exit code.
2. O bridge Python encaminha JSONL para o `codex.exe app-server` oficial.
3. Um controle em `127.0.0.1`, autenticado por segredo aleatório de 256 bits,
   recebe somente access token e account ID. Refresh token e id token não cruzam
   esse socket.
4. Métodos que iniciam trabalho ficam retidos enquanto uma troca está em curso.
   A identidade só muda depois que requests pendentes e turnos identificados por
   `(threadId, turn.id)` terminam.
5. O app-server confirma o login primeiro; só então o CCX grava `auth.json`, store
   e `last_switch`. Falha no commit local reaplica a conta anterior antes de abrir
   a barreira.
6. Dois bridges no mesmo `CODEX_HOME` recusam a operação inteira. Paralelismo usa
   perfis isolados, não hot-swap compartilhado.

## Rodada 1

O revisor executou as quatro suítes, validou o app-server real e bloqueou o lote
por um defeito no instalador:

| ID | Gravidade | Achado | Tratamento |
| --- | --- | --- | --- |
| CCX-1 | P1 | Um `{` em comentário de cabeçalho podia ser confundido com a raiz JSONC; as settings eram inseridas no comentário e o backup era descartado. | Scanner consciente de comentários/BOM, validação da raiz, backup persistente e teste de restauração byte a byte. |
| CCX-2 | P2 | Resposta do cliente com ID repetido podia sobrescrever um request de trabalho e prender a barreira. | Apenas requests com `method` entram no mapa; notificações de trabalho sem ID são recusadas; regressões para os dois casos. |
| CCX-3 | P3 | Cache legado sem `inactive` podia ser aceito como leitura atual do slot recém-ativado. | Ausência do campo passa a significar inativo; Claude e Codex forçam leitura fresca. |
| CCX-4 | P3 | O instalador escrevia vírgula sobrando em `{}`. | A inserção olha o próximo token JSONC e omite a vírgula em objeto vazio. |

Também foi fechada a incerteza sobre o payload de limite por modelo: o parser
aceita tanto `percent` quanto `utilization`.

## Rodada 2

O protocolo da revisão restringiu a decisão formal ao bloqueador CCX-1. O Opus
reproduziu o mesmo arquivo com comentário contendo `{` e confirmou:

- settings inseridas dentro do objeto raiz;
- JSONC parseável;
- comentário preservado;
- backup persistente durante a instalação;
- uninstall restaurando o original byte a byte;
- recusa fail-closed sem alteração para raiz array, arquivo só com comentário e
  comentário de bloco incompleto;
- comportamento correto com comentário de bloco, BOM, comentário dentro da raiz,
  reinstalação e valor alterado pelo usuário.

Decisão formal: **FIX_CONFIRMADO**.

As ressalvas CCX-2 a CCX-4 ficaram fora do veredito formal da rodada 2, como exige
o protocolo, mas suas regressões fazem parte das mesmas suítes reexecutadas pelo
Opus e passaram. O fix de CCX-2 permanece deliberadamente fail-closed: não há
expiração temporal artificial de request pendente, pois isso poderia liberar uma
troca durante trabalho cuja conclusão não foi confirmada.

## Evidências

| Verificação | Resultado |
| --- | --- |
| `python test_ccx.py` | 59/59 passaram |
| `python test_ccx_codex.py` | 39/39 passaram |
| `python test_ccx_profile.py` | 4/4 passaram |
| `python test_ccx_codex_bridge.py` | 18/18 passaram |
| `python -m py_compile ...` | passou |
| parser do PowerShell | passou |
| `git diff --check` | passou |
| Gitleaks no worktree | nenhum vazamento |
| Gitleaks no histórico | nenhum vazamento em 12 commits |
| hot-login no app-server real | dois logins sintéticos, mesmo processo, sem prompt ou consumo de cota |
| launcher/Job Object | stdio, argv, exit code e encerramento dos filhos confirmados |

O artefato bruto da rodada 2 registra `claude-opus-5` como modelo canônico, com
23.842 tokens de saída, `is_error=false` e sem fallback de modelo. O Haiku listado
pelo CLI consumiu apenas 15 tokens auxiliares e não produziu o parecer.

## Instalação operacional

Depois da aprovação, o instalador foi executado nas settings reais:

- `chatgpt.cliExecutable` aponta para o launcher CCX;
- `settingsSync.ignoredSettings` contém essa chave;
- backup e estado de instalação existem;
- o launcher resolve `codex-cli 0.153.0`;
- o app-server que executava esta sessão não foi encerrado ou recarregado.

Por isso o processo atual ainda aparece como um app-server direto e nenhum bridge
está vivo. É o estado esperado: `Developer: Reload Window` deve ser executado uma
única vez, manualmente, quando puder encerrar a sessão atual. Depois desse reload,
`ccx_codex switch N` consegue trocar a conta sem reiniciar os turnos seguintes.

## Limites conhecidos

- A configuração `chatgpt.cliExecutable` e `chatgptAuthTokens` são interfaces
  experimentais do Codex e podem exigir adaptação em uma versão futura.
- O recurso atual é Windows x64 e requer Python 3.10+ e o compilador C# do .NET
  Framework.
- Não migra uma requisição já iniciada e não oferece identidades distintas por
  thread dentro do mesmo app-server.
- O ciclo com arquivo UTF-8 contendo BOM preserva o conteúdo, mas normaliza a
  codificação para UTF-8 sem BOM; esse é também o formato normalmente gravado pelo
  VS Code.

## Conclusão

O lote está apto para commit e publicação. A rotação evita martelar contas
inativas, destrava imediatamente em uma conta disponível e passa a poder atualizar
um app-server Codex persistente com barreira de turno, commit transacional e
rollback. Não há bloqueador conhecido de execução, credencial ou segurança.

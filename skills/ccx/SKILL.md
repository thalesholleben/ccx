---
name: ccx
description: >-
  CCX: contas Claude/Codex, limites, margens e despacho de agentes em perfis isolados.
  ALWAYS invoke ANTES de invocar outro agente, executor, raia ou revisor, e ao operar
  contas ou limites pelo CCX. Ler a skill não autoriza delegação não solicitada.
---

# CCX para agentes

Antes de invocar outro agente, leia esta skill e confira o estado necessário ao
despacho. Ela define **como executar uma delegação já autorizada**, não quando
inventar uma delegação. Proibições de recursão ou de ferramentas da sessão prevalecem.

## 1. Localizar e escolher o caminho

O instalador grava `installation.json` ao lado desta skill, com `repo` e `cli`.
Leia esse arquivo para localizar o CCX; não adivinhe caminhos pessoais. Sem ele,
use o clone explicitamente informado pelo usuário ou `CCX_REPO`. Se não localizar,
informe a ausência. Não instale nem cadastre contas para resolver isso por conta própria.

Se existir `references/local.md` nesta instalação, leia também: contém somente
as preferências do operador. Essa referência é opcional e não faz parte do pacote público.

Nos exemplos abaixo, execute a partir da pasta do CCX; `--cwd` é a pasta do trabalho.

```powershell
python ccx-fleet.py status --json
python ccx-fleet.py service status
```

- **Frota pronta:** selecione o provedor/modelo pedido e despache pela frota.
- **Frota sem contas/perfis autenticados:** não envie uma tarefa que ficará na fila
  indefinidamente. Informe o login pendente. Um executor legado já autorizado pode
  continuar sendo usado, deixando explícito que ele está fora da frota.
- **Executor nativo da plataforma:** ferramentas de subagente da IDE não passam
  automaticamente pelo CCX e não oferecem escolha de conta por esta skill. Não
  prometa isolamento de contas que o executor não implementa.
- **Cross-plan/review e outros protocolos próprios:** preserve o runner oficial,
  as guardas de recursão, modelo/esforço e teto de rodadas. Ler esta skill não
  permite substituí-los por um `fleet run` que perca seus contratos.

## 2. Despachar

Seleção automática é o padrão: não escolha conta só pelo percentual usado.

```powershell
python ccx-fleet.py run claude --prompt-file tarefa.md --cwd C:\work\app --request-id revisao-123 --timeout 900
```

Quando o usuário pedir **a conta X**, use o ID cadastrado e `--cell`:

```powershell
python ccx-fleet.py run claude --cell ID_DA_CONTA --prompt-file tarefa.md --cwd C:\work\app --request-id revisao-123 --timeout 900
```

O cadastro gera um ID interno e guarda o nome escolhido como label. Consulte
`status --json`: `id` é o identificador para comandos, `display_name` é editável.
Alterar o label preserva login, perfis, tarefas e o ID.

`--cell` restringe esta tarefa à conta indicada. Não muda o login global e não faz
fallback para outra conta se ela estiver indisponível. Confira antes se o ID existe,
o provedor é compatível, há perfil autenticado e a medição é recente. A mesma chave
`--request-id` só pode ser reutilizada para conteúdo e opções idênticos.

Use `--permission write` apenas quando a tarefa autorizar alterações. O padrão é
leitura: Claude recebe Read/Glob/Grep, sem Bash; Codex usa sandbox read-only. Um
teste que precisa executar shell pode exigir outro modo, dentro da autorização.
Não mude modelo, esforço, permissão ou provedor para contornar uma recusa.

`run` inicia o serviço antes de enfileirar. `submit` só enfileira. Timeout do cliente
**não cancela** a execução: guarde o ID e consulte/espere/cancele explicitamente.
Se o orquestrador precisa cancelar ao atingir o prazo, use o exemplo executável
`examples/dispatch-agent.py` e confira o encerramento, sem disparar outro executor
enquanto houver dúvida sobre o primeiro.

Comandos completos e códigos de saída: [references/commands.md](references/commands.md).

## 3. Como funciona por dentro

Uma **célula** é uma conta/pool de limites. Um **worker** é um perfil OAuth
independente, com um agente por vez. Vários workers da mesma conta compartilham cota.
Ilimitado significa ausência de teto numérico global/por conta, não infinitas
credenciais, memória, cota ou processos no mesmo perfil.

Fila, escolha de conta, reserva e ticket são atômicos em SQLite. Cada runner é
independente do painel e do serviço. Fechar a UI preserva execução; parar o serviço
impede novos despachos e deixa runners vivos concluírem. Não há transferência
automática de uma tarefa em andamento para outra conta.

Pesos de sessão: Claude Pro 1, Max 5 5, Max 20 20; Codex Plus 1 e Pro 5.
Codex Pro x20 não é oferecido nesta versão. Peso semanal padrão 1 e margem 10%.
Custo padrão 15 pontos x1: reserva 15 / 3 / 0,75 pontos de sessão nos pesos x1 / x5 / x20,
mas 15 pontos semanais com o peso semanal padrão. Percentual igual não significa
capacidade igual, e semanal/modelo podem vetar a conta com muita folga de sessão.

Limites, frescor, reservas e falhas: [references/internals.md](references/internals.md).

## 4. Estratégia de escolha

- Deixe o escalonador distribuir tarefas comuns pela folga ponderada e reservas.
- Para trabalho longo/caro, informe custo conservador com `--cost`; prefira células
  com folga nas janelas aplicáveis e perfis livres. Max 20 não é garantia de cota semanal.
- Restrinja com `--cell` para pedido explícito, canário ou configuração específica
  daquela conta. Não fixe um slot global para simular afinidade por tarefa.
- Escritas concorrentes exigem pastas/worktrees independentes. Na mesma pasta ou
  pastas sobrepostas, o CCX serializa quando houver escritor.
- Se a cota for desconhecida, vencida, bloqueada ou o login falhar, diagnostique;
  nunca interprete isso como 0% usado nem repita o prompt cegamente.

Exemplos de decisão e transição do legado: [references/strategies.md](references/strategies.md).

## Invariantes

Não copie tokens entre perfis, não renove grants globais por scripts improvisados,
não abra perfil gerenciado fora do CCX e não edite SQLite/arquivos de autenticação
manualmente. Use o CLI para mudanças. Nunca coloque token, e-mail real, prompt
privado ou dados de contas em logs públicos, issues, screenshots ou commits.

`ccx.py stats` consulta o modo legado; a frota usa `ccx-fleet.py status`. Troca
global não migra automaticamente clientes persistentes nem tarefas da frota.

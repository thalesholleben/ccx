# Frota por células

## O que muda

Cada célula representa uma conta e seu pool de limites. Cada worker tem uma home
OAuth própria e aceita um CLI por vez. A fila escolhe a conta antes de iniciar o
agente, com reserva atômica de capacidade em SQLite. O serviço, os runners e o
painel são processos separados. Fechar o painel ou o cliente de espera preserva
as tarefas. Parar o serviço interrompe novos despachos; runners vivos concluem.

Esta é a interface principal. Sessões abertas no VS Code e comandos antigos
continuam fora da frota. Para distribuir trabalho, despache pelo `ccx-fleet.py`.
Nenhum wrapper no PATH, hook global ou tarefa automática de logon foi instalado.

## Começar no Windows

Abra `ccx-panel.cmd` com dois cliques. Cadastre a conta, selecione a linha, faça
login e inicie o serviço. O CLI oferece o mesmo fluxo:

O cadastro pede apenas **nome, provedor e plano**. O nome é um label; o ID interno
é único e gerado automaticamente. Em **Editar conta**, altere o nome e/ou plano
sem trocar o ID, login, perfis ou vínculos de tarefas. Nos comandos abaixo,
substitua `ID_DA_CONTA` pelo ID impresso no cadastro ou no status. Peso por sessão vem do plano,
peso semanal começa em 1 e margem reservada em 10%. Esses parâmetros são internos
no painel. Alterar o plano recalcula o peso por sessão; editar só o nome preserva
o peso atual. Não há teto numérico de
agentes, global ou por conta; cota, perfis autenticados disponíveis e exclusão de
escrita na mesma pasta continuam governando o despacho.

No Windows, o ícone CCX fica na bandeja (pode aparecer no grupo de ícones ocultos).
Clique para mostrar/ocultar o painel. Botão direito oferece **Abrir painel**,
**Ocultar painel** e **Sair da bandeja (agentes continuam)**. O X da janela apenas
oculta o painel. Abrir o launcher novamente traz a mesma janela, sem duplicar o
ícone. Sair da bandeja encerra só a interface; serviço e tarefas continuam vivos.
Após reiniciar o Windows, abra o launcher novamente. Não foi instalado autostart.
Se o Explorer não disponibilizar a bandeja, o painel mantém fechamento normal.

```powershell
python ccx-fleet.py cell add pessoal claude --plan pro
python ccx-fleet.py cell add equipe claude --plan max5
python ccx-fleet.py cell add principal claude --plan max20
python ccx-fleet.py cell login ID_DA_CONTA
python ccx-fleet.py cell login ID_DA_CONTA
python ccx-fleet.py cell login ID_DA_CONTA
python ccx-fleet.py service start
python ccx-fleet.py status
```

Cada cadastro cria **um** worker. Para usar dois agentes simultâneos na mesma conta,
adicione e autentique outro worker. Não há campo para limitar agentes: cada perfil
continua exclusivo de uma execução por vez.

```powershell
python ccx-fleet.py worker add ID_DA_CONTA
python ccx-fleet.py cell login ID_DA_CONTA --worker ID_DO_WORKER
python ccx-fleet.py cell configure ID_DA_CONTA --plan max20
python ccx-fleet.py cell pause ID_DA_CONTA
python ccx-fleet.py cell resume ID_DA_CONTA
```

O login usa o CLI oficial com home dedicada. Não copie tokens dos arquivos globais.
Login adicional na mesma conta depende da coexistência de grants permitida pelo
provedor. Antes de ampliar workers, valide que os demais perfis e a sessão global
continuam autenticados. Esse canário exige login humano e não foi substituído por
testes sintéticos. Conta Codex: `cell add "Nome visível" codex --plan custom --weight 1`;
o comando imprime o ID para o login e o despacho. Os presets do Codex são
`--plan plus` (x1, padrão) e `--plan pro` (x5). Pro x20 fica fora do catálogo desta
versão. O CLI permite custom com peso explícito, sem inferir assinatura de uso zero.

## Remover uma conta

Selecione a conta e use **Remover conta**. A confirmação explica que cadastro e
perfis locais, incluindo seus logins, serão apagados. O histórico e os resultados
das tarefas ficam preservados com a indicação de conta removida. Equivalente no CLI:
`python ccx-fleet.py cell remove ID_DA_CONTA --yes`.

Login, perfil bloqueado/ocupado, execução ainda viva ou tarefa na fila com afinidade
explícita impedem a remoção. Finalize ou cancele essas tarefas antes. A operação não
cancela assinatura nem revoga tokens remotamente. Se o Windows impedir a limpeza
de um arquivo, o CCX informa remoção parcial e registra `cell_profile_cleanup_failed`;
o campo `entity` do evento identifica cada diretório residual relativo à pasta
privada da frota. Todos os perfis são tentados. Perfil já ausente não bloqueia a
remoção; marcador de posse ausente/corrompido preserva os arquivos e registra a
limpeza pendente. Perfil redirecionado para outro caminho é recusado.

## Enviar e acompanhar

```powershell
python ccx-fleet.py submit claude --prompt-file .\tarefa.md --cwd C:\projetos\app --title "Revisar testes"
python ccx-fleet.py run claude --prompt-file .\tarefa.md --cwd C:\projetos\app --permission write --model claude-opus-5-5 --effort high
python ccx-fleet.py wait ID_DA_TAREFA --timeout 600
python ccx-fleet.py cancel ID_DA_TAREFA
python ccx-fleet.py status --json
python ccx-fleet.py service status
python ccx-fleet.py service stop
```

Sem `--prompt-file`, lê stdin. `submit` apenas enfileira; `run` inicia o serviço e
espera. `wait --timeout` retorna 124 e deixa a tarefa viva. `cancel` solicita o
encerramento, que só é confirmado após a morte do runner. Estado ambíguo recebe
`needs_attention`, sem repetir prompt automaticamente. Resultados privados em
`~/.ccx/fleet/jobs/<id>/result.json`. `--request-id` deduplica submissões iguais;
reutilizar a chave com outro conteúdo é recusado.

Permissão padrão `read-only`: Claude só recebe Read/Glob/Grep em plan mode; Codex
usa sandbox read-only. `write` usa acceptEdits no Claude e workspace-write no Codex.
Não concede bypass. Uma ferramenta que exija aprovação não disponível pode falhar.
Home de autenticação isolada não é isolamento de filesystem. Diretórios iguais,
pais e filhos não recebem trabalhos concorrentes se um deles puder escrever.
Leitores podem coexistir. Use worktrees previamente criadas para branches paralelas.

Integração executável para orquestradores:

```powershell
python examples\dispatch-agent.py --provider claude --work-id revisao-123 --cwd C:\projetos\app --prompt-file .\tarefa.md --model claude-opus-5-5 --permission read-only --timeout 1800
```

Esse exemplo preserva as guardas de recursão e cancela explicitamente ao exceder o
prazo, aguardando confirmação. O executor global e o runner de cross-review seguem
inalterados; não há migração automática de rotinas para a frota.

## Como a capacidade é calculada

| Plano Claude | Peso por sessão | Folga com 50% de uso e 10% de margem |
|---|---:|---:|
| Pro | 1 | 0,40 equivalente x1 |
| Max 5 | 5 | 2,00 equivalentes x1 |
| Max 20 | 20 | 8,00 equivalentes x1 |

Um job com `--cost 15` reserva 15 pontos na Pro, 3 na Max 5 e 0,75 na Max 20.
O custo é estimativa do operador, não contagem de tokens. A janela semanal e os
limites de modelo podem vetar o job. O peso semanal começa em **1**, separado do
peso por sessão: configure `--weekly-weight` somente com evidência da sua conta.
Com os padrões custo 15, margem 10 e peso semanal 1, um semanal acima de 75%
segura novos jobs mesmo na Max 20. Essa proteção também pode limitar o paralelismo
antes da janela por sessão. Calibre custo e peso semanal com medições reais.
O serviço compara pressão da reserva sobre a folga ponderada e usa desempate por
reset/próximo despacho. Não impõe limite de agentes: despacha enquanto existirem
perfis livres e capacidade. Os antigos tetos de 8 globais e por conta foram
removidos. O schema 3 preserva contas, jobs e reservas; mantém as colunas legadas
`max_active` com valor 0 (sem teto), sem usá-las na decisão.

Reservas de tarefas ativas continuam inteiras mesmo quando uma medição já incluiu
parte do uso. Reservas concluídas só saem depois de 60 segundos e uma nova leitura
iniciada após esse prazo. Isso é conservador e pode segurar a fila temporariamente.
Uma tendência recente de consumo também reduz a capacidade admitida.

O coletor usa GET de limites: 180 segundos em célula ativa, 240 em ociosa com fila
compatível ou em erro. Sem fila nem tarefa ativa, não consulta uso nem renova OAuth.
Snapshot pode admitir por até 600 segundos. Reset vencido exige nova leitura.
Reset nulo com uso conhecido é janela sem reset anunciado, sujeito ao mesmo TTL;
não vira zero. Uso nulo é ignorado, e vários limites da mesma família de modelo
são agregados pelo maior consumo e reset mais tarde.
401/403 bloqueiam o perfil para novo login. 429 preserva medição conhecida até seu
TTL, nunca vira uso zero. Estado desconhecido segura a fila. O CCX só renova OAuth
de worker ocioso sob o mesmo lock de SO usado durante CLI/login. Renovação preserva
os demais campos do arquivo, incluindo MCP. Nenhum grant global é renovado aqui.
Revogação encontrada no preflight ou erro tipado 401/403 do CLI bloqueia o worker;
as próximas tarefas podem usar os demais. Falha transitória impõe 240 segundos
de backoff. Não há nova tentativa automática da tarefa que falhou.

## Status e painel

`ccx-fleet.py status` mostra a frota no terminal; `--no-color` ou `NO_COLOR`
removem cores, e `--json` fornece dados para integrações. `ccx.py stats` e
`ccx_codex.py status` são status do legado, não da frota.

O painel abre em **Capacidade**, com indicadores slim e todas as contas em cards
compactos, uso de sessão/semanal e folga estimada. Estado sem leitura nunca aparece
como 0%. Não há hero nem criação manual de tarefas. A área Tarefas acompanha o que
os agentes e integrações enviam pelo CLI. Contas e Atividade mantêm os controles
detalhados. O visual usa grafite, verde menta, tipografia
local e tabelas com rolagem. Não usa navegador,
servidor HTTP nem dependências de frontend.

## Operação e limites

Estado em `~/.ccx/fleet/fleet.sqlite3`, perfis em `profiles/<provider>/<worker>`.
Não abra um perfil gerenciado diretamente em outro CLI: isso violaria a exclusão
de refresh. Os perfis recebem apenas um arquivo de instrução apontando às skills
globais. Hooks, settings, MCP privado, auth e histórico não são copiados. Configure
MCP necessário no perfil enquanto estiver ocioso; não instale hooks de troca global.
O ambiente do CLI herda somente caminhos e variáveis de runtime aprovadas, incluindo
CLAUDE_CODE_GIT_BASH_PATH. Guardas de recursão vêm exclusivamente da tarefa.
Tokens de GitHub, cloud e outros serviços do processo que iniciou o daemon não
são herdados. Ferramentas que precisam dessas integrações devem usar os arquivos
locais da integração ou a configuração explícita do perfil. O Codex npm nativo
tem preferência ao binário da extensão. Esforço: Claude low/medium/high/max;
Codex low/medium/high/xhigh. Família de limite desconhecida restringe todos os
modelos até haver mapeamento comprovado.

O diagnóstico distingue exit não zero, erro relatado, terminal ausente e resultado
vazio. Stderr bruto é descartado para não registrar segredos. `wait` com tarefa
na fila e serviço morto sai com código 3 e orienta iniciar o serviço. `run` inicia
o serviço antes de criar a tarefa, evitando execução tardia após bootstrap falho.
Exceção de ciclo registra evento classificado e tenta novamente; não mata runners.

Windows: filhos normalmente usam breakaway/detach. Dentro de um Job Object que
proíbe breakaway, usa tarefa do Agendador **somente sob demanda**, usuário atual,
Interactive/Limited, sem triggers. Confirma o heartbeat antes de anunciar serviço
iniciado. Falha de bootstrap orienta abrir o launcher pelo Explorer. Tarefas de
runner são removidas após confirmar morte; a tarefa do serviço é reutilizável.
Não promete continuidade após reiniciar Windows ou logout. macOS/Keychain não está
validado; esta implementação foi exercitada no Windows.

`stop` é rollback operacional: para a admissão, deixa runners concluir e permite
voltar aos comandos legados. Preserve o banco/perfis para consultar resultados.
Eventos operacionais contêm IDs, estados e erros classificados; prompt, e-mail,
tokens e saída de ferramentas não entram nesses eventos nem no snapshot do painel.
Arquivos privados do job podem conter informação sensível. Não os versione.

Validação: `python test-fleet.py` e as cinco suítes do AGENTS.md. Testes de UI:
`python tests/panel-smoke.py` (captura opcional de imagem exige Pillow somente no
teste) e `python tests/tray-smoke.py` (Windows, incluindo serviço independente).
Fixtures executam processos reais, sem provedores ou credenciais reais.

Esforços listados aqui são os aceitos pelo CCX. Se o pedido usar outro nível,
informe que ainda não é suportado pela frota; não reduza o esforço silenciosamente.

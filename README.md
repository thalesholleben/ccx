# ccx

Monitora a cota das contas Claude Code e troca de conta antes de bater o limite.

Python stdlib puro, sem instalar nada. Nasceu para resolver um incômodo concreto:
com duas assinaturas Pro, você fica monitorando na mão qual delas ainda tem cota,
fazendo `/logout` e `/login` no meio do trabalho, e ainda desperdiça cota semanal
que ia expirar sem ser usada.

**Não existe ping de aquecimento aqui, de propósito.** A leitura de cota não manda
prompt nenhum. Ver [Termos de uso](#termos-de-uso).

Também tem um módulo para contas do Codex CLI (ChatGPT), ver
[Módulo Codex (ccx_codex)](#módulo-codex-ccx_codex).

---

## Sumário

- [Pré-requisitos](#pré-requisitos)
- [Setup](#setup)
- [Comandos](#comandos)
- [Estratégias de troca](#estratégias-de-troca)
- [Intervalo de checagem](#intervalo-de-checagem)
- [No VS Code](#no-vs-code)
- [Como funciona por dentro](#como-funciona-por-dentro)
- [Problemas conhecidos e como diagnosticar](#problemas-conhecidos-e-como-diagnosticar)
- [Limitações](#limitações)
- [Termos de uso](#termos-de-uso)
- [Teste](#teste)
- [Módulo Codex (ccx_codex)](#módulo-codex-ccx_codex)

---

## Pré-requisitos

Duas ou mais contas Claude Code, e cada uma precisa ter sido logada pelo menos uma
vez nesta máquina para o `add` capturar a credencial.

**A pré-condição que realmente importa:** as contas precisam estar em organizações
diferentes. Contas com o mesmo `organizationUuid` compartilham um pool de cota só,
então trocar entre elas não resolve nada. O `add` avisa quando detecta isso. Para
conferir na mão, `oauthAccount.organizationUuid` em `~/.claude.json`.

Plataforma: Windows e Linux funcionam. No macOS o Claude Code guarda a credencial no
Keychain e o `add` não encontra (ver [Limitações](#limitações)).

## Setup

```bash
# logue com a primeira conta no Claude Code, depois:
python ccx.py add

# /logout, /login com a segunda conta, depois:
python ccx.py add

# confira
python ccx.py status
```

O `add` é idempotente: rodar de novo com a mesma conta ativa atualiza o slot
existente em vez de criar um duplicado, e limpa a marca de token morto se houver.

## Comandos

| Comando | O que faz |
| --- | --- |
| `ccx add [slot]` | Captura a conta logada agora. Sem argumento, usa o próximo número livre |
| `ccx status` | As contas lado a lado: 5h, 7d, quando cada janela reseta, e a recomendação |
| `ccx stats` | Status consolidado de todas as contas Claude Code e Codex CLI |
| `ccx switch [slot]` | Troca manual. Sem argumento, rotaciona para a próxima |
| `ccx auto` | O monitor: acompanha a cota e troca sozinho |
| `ccx hook` | Checagem silenciosa para o evento `Stop` do Claude Code |

Flags globais, válidas em qualquer subcomando:

| Flag | Padrão | Efeito |
| --- | --- | --- |
| `--strategy consume-first\|best` | `consume-first` | Ver [Estratégias](#estratégias-de-troca) |
| `--threshold N` | `85` | Percentual em que a conta deixa de ser candidata |

Flags do `auto`:

| Flag | Padrão | Efeito |
| --- | --- | --- |
| `--cooldown N` | `300` | Segundos mínimos entre duas trocas, evita pingue-pongue |
| `--poll N` | `0` | Força intervalo fixo em segundos. `0` usa o dinâmico |
| `--once` | | Uma checagem só e sai, para uso em agendador |

Códigos de saída do `--once`: `0` trocou, `1` erro de configuração, `2` nada a
fazer, `3` todas as contas travadas.

**Uma instância por máquina.** Se já existe um `auto` rodando, o segundo avisa e sai
com código 0. Sem isso, abrir a pasta em três janelas do VS Code subiria três loops
triplicando o tráfego e disputando a mesma troca.

## Estratégias de troca

Primeiro, dois conceitos que não são a mesma coisa:

- **Utilizável:** a janela que aperta está abaixo de 100%. A conta consegue atender.
- **Candidata:** a janela que aperta está abaixo do `--threshold`. Vale trocar para ela.

"Janela que aperta" é a maior entre 5h e 7d, porque é a que bloqueia primeiro. Uma
conta com 5h em 0% e semanal em 97% está tão bloqueada quanto o contrário.

**`consume-first`** (padrão): entre as candidatas, escolhe a de reset semanal mais
próximo. Cota semanal é perecível, então a lógica é gastar primeiro a que vai virar
pó. Troca mais, aproveita mais.

**`best`**: entre as candidatas, escolhe a de maior folga. Troca menos, mas
desperdiça cota semanal que ia expirar.

**Quando nenhuma conta é candidata**, cai para a utilizável de menor utilização. O
limiar existe para trocar antes de bater, não para te deixar parado numa conta
travada tendo outra que ainda atende. Se todas estiverem em 100%, aí não há o que
fazer e ele avisa.

Conta com cota desconhecida (erro de rede, token morto) nunca é escolhida
automaticamente, mas segue sendo alvo válido de um `switch` explícito.

## Intervalo de checagem

Polling só serve para decidir uma troca, e decisão de troca exige **duas contas
utilizáveis**. Todo o intervalo sai dessa observação:

**Duas ou mais utilizáveis:** faixa com jitter, 180 a 240s normalmente, apertando
para 100 a 120s quando a janela de **5h da conta ativa** passa de 70%.

O gatilho é o 5h de propósito. É a única janela que se move rápido dentro de uma
sessão. O semanal sobe devagar e leva dias para resetar, então usá-lo aqui
prenderia o poll na faixa apertada por dias inteiros sem que nada estivesse por
acontecer. Um poll de 4 minutos não perde nada de relevante no semanal.

**Menos de duas utilizáveis:** não existe decisão a tomar até alguma resetar, e o
`resets_at` da API diz exatamente quando. Dorme até lá em vez de checar, com teto de
1h (seguro barato caso o horário venha errado ou mude) e piso de 60s (evita busy
loop se o `resets_at` estiver no passado por dado velho). Na prática isso troca umas
25 requisições por 1.

Uma conta travada volta a atender quando **todas** as suas janelas em 100% resetarem,
então quem manda é o reset mais tarde entre elas. Entre contas travadas, acorda
quando a primeira volta.

**Erro em qualquer conta:** cai para a faixa larga. Se o endpoint devolveu 429,
insistir de 100 em 100s só piora.

O jitter também evita um batimento perfeitamente periódico, que é padrão mais fácil
de detectar do que poll irregular.

### Checagem ao terminar cada resposta

Polling sozinho tem uma janela inevitável: uma resposta longa pode fazer a cota
saltar entre duas consultas. O comando `ccx hook` existe para o evento `Stop` do
Claude Code e consulta a cota assim que cada resposta termina. Se uma conta cruzou
o ponto de troca durante o turno, a próxima mensagem já usa a conta escolhida.

O hook é silencioso, respeita o mesmo cooldown e usa o mesmo `store.lock` do
monitor. Ele não manda prompt e não consome cota; apenas acrescenta uma leitura de
usage por conta ao fim de cada turno. O `auto` continua rodando como fallback para
resets e mudanças fora de uma sessão.

Configuração de usuário em `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/caminho/para/ccx.py\" hook",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

Hooks são carregados ao abrir a sessão. Depois de instalar ou alterar essa
configuração, encerre e abra o Claude Code novamente.

## No VS Code

As tasks `ccx auto` e `ccx_codex auto` sobem junto com a pasta do repositório e o
VS Code encerra o processo quando você fecha a janela, porque elas são filhas
diretas do terminal integrado. O ciclo de vida já é o do VS Code nas duas
pontas, sem precisar de código.

Definidas em [`.vscode/tasks.json`](.vscode/tasks.json) com `runOn: folderOpen`.
A apresentação é `silent`: a aba fica escondida enquanto está tudo bem e aparece
sozinha se houver erro.

**Isso exige `"task.allowAutomaticTasks": "on"` no settings.json de usuário**, em
`%APPDATA%\Code\User\settings.json`. Não funciona no settings.json da pasta: o VS
Code ignora essa chave vinda do workspace de propósito, senão qualquer repositório
clonado poderia se autorizar a executar comandos na abertura.

Só vale na **abertura da pasta**, não em Reload Window. Para subir na mão,
`Ctrl+Shift+P` → `Tasks: Run Task` → `ccx auto`.

Fora do VS Code, `ccx-auto.cmd` faz o mesmo com um duplo clique, de qualquer
diretório.

## Como funciona por dentro

### De onde vem a cota

`GET https://api.anthropic.com/api/oauth/usage`, com `Authorization: Bearer <token>`
e o header `anthropic-beta: oauth-2025-04-20`. É a mesma fonte que o `/usage` do
Claude Code consome.

Leitura pura: não manda prompt, não consome cota e **não abre a janela de 5h**. A
resposta traz `five_hour` e `seven_day`, cada um com `utilization` (percentual) e
`resets_at` (ISO 8601).

### O que a troca escreve

Dois arquivos, e nos dois ela é cirúrgica:

| Arquivo | O que muda |
| --- | --- |
| `~/.claude/.credentials.json` | Só o bloco `claudeAiOauth` e o `organizationUuid` |
| `~/.claude.json` | Só o `oauthAccount` (a identidade que o Claude Code exibe) |

**Por que cirúrgica e não copiando o arquivo:** o `.credentials.json` guarda também
`mcpOAuth`, onde vivem os tokens OAuth dos servidores MCP. Uma troca que sobrescreve
o arquivo inteiro derruba esses logins toda vez. Existe teste que falha se isso
regredir (`test_swap_preserva_mcp`).

Toda escrita é atômica: arquivo temporário e `os.replace`, para nunca deixar um
JSON truncado se o processo morrer no meio.

### O lock, que é a parte séria

Toda escrita acontece segurando o lock do próprio Claude Code. O protocolo, lido do
código dele:

- O artefato é um **diretório** em `<alvo>.lock` (`~/.claude.lock`,
  `~/.claude.json.lock`). A atomicidade do `mkdir` é o mutex.
- Considera-se morto quando o mtime passa de 10s. Quem segura toca o mtime a cada
  5s para provar que está vivo, e um lock morto pode ser tomado.
- O Claude Code tenta 5 vezes com sleeps de 1 a 2s antes de desistir, então segurar
  por meio segundo é totalmente cooperativo.

**Sem isso a ferramenta falha em silêncio.** O fluxo de refresh do Claude Code é ler
a credencial, ir na rede e salvar, tudo sob `~/.claude.lock`. Uma troca que caísse
dentro dessa janela seria sobrescrita pelo token da conta velha, e você não veria
erro nenhum, só continuaria na conta errada. Sob o lock, a releitura dele enxerga a
credencial nova (não expirada) e ele aborta o próprio refresh.

O mesmo mecanismo de lock é reusado para a trava de instância única do `auto`, em
`~/.ccx/auto.lock`, com timeout 0 para falhar na hora em vez de esperar.

### Concorrência entre os próprios comandos

Existe um segundo lock, `~/.ccx/store.lock`, separado do lock de instância única
do `auto`. Ele serializa **toda** operação que renove token ou grave o store:
`collect` (usada por `status` e pelo `auto`), `switch` e `add`.

Isso não é zelo teórico, é cicatriz. A trava de instância única protegia só o
loop do `auto`, mas `status` também renova token e grava. Rodar `ccx status` num
terminal enquanto o `auto` rodava fazia os dois renovarem o mesmo refresh token
ao mesmo tempo. A rotação no servidor invalida a cópia de quem perdeu a corrida,
e a credencial guardada morre com `invalid_grant`, sem erro visível até você
tentar voltar para aquela conta e descobrir que ela não existe mais.

Pelo mesmo motivo, `apply_slot` segura os **dois** locks do Claude Code durante a
troca inteira, em vez de um por escrita. Entre gravar a credencial e gravar o
perfil existe um instante em que o token é de uma conta e o `oauthAccount` é de
outra. Quem lesse a identidade nessa janela concluiria a conta errada, e o
`sync_active_slot` seguinte copiaria o token de uma conta para o slot da outra.

Ordem de aquisição, sempre a mesma para não travar: `store.lock` primeiro, depois
os locks do Claude Code.

### Leitura de arquivo: corrompido não é vazio

`read_json` devolve `{}` só quando o arquivo **não existe**. JSON inválido levanta
`CorruptFile` e aborta a operação.

Confundir os dois é destrutivo: o passo seguinte reescreve o arquivo e leva junto
o que não foi lido. Num `.credentials.json` truncado por uma escrita interrompida,
isso apagaria o `mcpOAuth` inteiro.

Pela mesma lógica, uma resposta 200 do endpoint de usage sem nenhuma janela
reconhecível levanta em vez de virar `{}`. Um dicionário vazio faria a conta
aparecer com 0% de uso, ou seja, ser eleita como a mais folgada de todas
justamente por não sabermos nada sobre ela.

### Identidade da conta ativa, e por que não pelo token

O slot ativo é identificado por **(e-mail, `organizationUuid`)** lidos do
`oauthAccount` em `~/.claude.json`, nunca comparando tokens.

O Claude Code rotaciona o refresh token da conta que está usando. Se a
identidade dependesse do token, ela se perderia na primeira rotação, e aí
**todo slot pareceria inativo, inclusive o vivo**. A consequência é a pior
possível: o `ccx` passaria a renovar o token da conta ativa por baixo do Claude
Code, os dois disputariam a rotação, e um invalidaria o refresh token do outro,
matando a credencial guardada.

Pelo mesmo motivo, a cada checagem a credencial viva é copiada de volta para o
slot ativo (`sync_active_slot`). Sem isso a cópia guardada envelhece enquanto a
conta é usada, e quando você tentasse voltar para ela o refresh responderia
`invalid_grant`.

### Certificados: o `ccx` prefere o bundle do certifi

O armazenamento de certificados do Windows carrega raízes legadas expiradas, e a
cadeia da Let's Encrypt falha por ali. Isso atinge `platform.claude.com`, onde
fica o endpoint de refresh, mas **não** atinge `api.anthropic.com`, que usa outra
CA. O sintoma é traiçoeiro: a leitura de cota funciona normalmente e só o refresh
quebra, então a conta inativa vai apodrecendo sem erro visível até morrer.

Por isso o contexto TLS usa `certifi.where()` quando o pacote está disponível, e
cai no padrão do sistema quando não está. Para conferir:

```bash
python -c "import ssl,socket; ssl.create_default_context().wrap_socket(socket.create_connection(('platform.claude.com',443)),server_hostname='platform.claude.com')"
```

Se isso levantar `CERTIFICATE_VERIFY_FAILED`, o `certifi` deixa de ser opcional:
`pip install -U certifi`.

### Regras de refresh de token

- **A conta ativa nunca é renovada.** O Claude Code é dono dela. Se os dois
  renovarem, um invalida o token do outro.
- **A inativa é renovada quando expira**, senão não há como ler a cota dela. O POST
  vai para `https://platform.claude.com/v1/oauth/token` com
  `grant_type=refresh_token` e o `client_id` público do Claude Code.
- **Refresh token rejeitado** (`invalid_grant` em 400, 401 ou 403) é permanente: o
  slot é marcado `MORTO`, sai da rotação e aparece assim no `status`. Insistir num
  token morto só gera ruído. Para recuperar, logue com aquela conta e rode
  `ccx add` de novo.
- **Qualquer outro erro é transitório** e volta a ser tentado na próxima checagem.

### Estado

`~/.ccx/accounts.json`, com os slots, e `last_switch` para o cooldown. Por slot:
e-mail, bloco `claudeAiOauth`, `oauthAccount` e `org_uuid`.

**Contém tokens OAuth. Nunca versione nem copie para fora da máquina.**

## Problemas conhecidos e como diagnosticar

**"A task não sobe com o VS Code."** Confira `task.allowAutomaticTasks` no
settings.json de **usuário**, não no da pasta. Depois teste na mão com
`Tasks: Run Task`. Se subir na mão mas não na abertura, o suspeito seguinte é
Workspace Trust.

**Saber se o `auto` está vivo:**

```bash
python -c "import os,time; p=os.path.expanduser('~/.ccx/auto.lock'); print('vivo' if os.path.isdir(p) and time.time()-os.stat(p).st_mtime < 10 else 'morto')"
```

Lock existindo com mtime velho significa processo morto sem limpar. A próxima
execução toma o lock automaticamente pela regra de staleness, não precisa apagar
na mão.

**Conta aparece com `?` na cota.** O `status` mostra o motivo ao lado (`HTTP 429`,
`timeout`, `token morto`). Um 429 no endpoint de usage derruba o poll para a faixa
larga sozinho.

**Trocou mas o Claude Code continua na conta antiga.** No Windows e no Linux a
credencial nova é lida na próxima requisição. Se estiver no meio de um turno, vale a
partir do turno seguinte.

## Limitações

- **macOS não é suportado no `add`:** lá o Claude Code guarda a credencial no
  Keychain, não em arquivo.
- **Reset banking do Codex não é usado.** A API da OpenAI expõe
  `rate_limit_reset_credits`, créditos redimíveis que resetam a janela atual
  antes da hora. O `ccx_codex` não lê nem redime esse crédito: redimir seria
  manipular o estado da conta, o mesmo motivo pelo qual não existe ping de
  aquecimento no módulo Claude. Fica para uma versão futura, só como leitura.
  Ver [Módulo Codex](#módulo-codex-ccx_codex).
- **Não considera limites semanais por modelo.** A API expõe isso num array
  `limits` com entradas `weekly_scoped`, que hoje é ignorado. Se você trabalha
  fixado num modelo e bate o limite dele antes das janelas gerais, esse ramo
  precisaria entrar na decisão.
- **Uma troca no meio de um turno em andamento** vale a partir do turno seguinte.

## Termos de uso

A ferramenta lê a API de usage com token OAuth de assinatura, o que a cláusula 3.7
dos Consumer Terms da Anthropic trata como acesso automatizado fora de API key. Não
há caso conhecido de enforcement contra isso, e é o que qualquer status line de cota
faz.

O que gera banimento é outra coisa: harness de terceiro, spoofing do harness e
revenda de acesso. Nada disso acontece aqui, quem fala com o modelo continua sendo o
Claude Code oficial, e a ferramenta só reposiciona qual credencial ele usa. Ter
várias contas pagas não viola os termos, isso foi dito publicamente por engenheiro
da Anthropic.

Por isso também não existe ping periódico para abrir a janela de 5h mais cedo: seria
script disparando prompt em assinatura, o caso que a cláusula descreve ao pé da
letra. Se quiser alinhar a janela ao seu dia, manda a primeira mensagem você mesmo.

## Teste

```bash
python test_ccx.py
```

Sem framework, só `assert`. 17 testes cobrindo:

- escolha de conta nas duas estratégias, e o fallback quando nenhuma é candidata
- cálculo de intervalo nos dois ramos (faixa com jitter e sono até o reset)
- execução silenciosa da checagem usada pelo hook `Stop`
- saída do `status` sem intervalo enganoso quando há troca pendente
- preservação do `mcpOAuth` na troca, e identidade não vazando entre slots
- identidade sobrevivendo à rotação de token feita pelo Claude Code
- JSON corrompido abortando em vez de virar arquivo vazio
- respostas inesperadas da API (200 sem `access_token`, usage sem janelas)
- `resets_at` sem fuso horário
- validação de flags, liberação do lock e escrita atômica

Vários desses nasceram de bugs que só apareceram rodando em produção, não de
casos imaginados. Se um deles quebrar, foi regressão de algo que já falhou uma
vez.

## Módulo Codex (ccx_codex)

Mesma ideia do `ccx.py`, para contas do Codex CLI (ChatGPT). É um arquivo
separado (`ccx_codex.py`) que **importa `ccx.py`** e reusa de lá a engine de
decisão (`pick_target`, `band_delay`, `next_wake`), a formatação de `status` e
os primitivos de IO (leitura/escrita atômica, lock de diretório). O que muda é
só o que é genuinamente diferente entre Claude Code e Codex CLI: formato do
arquivo de credencial e protocolo da API de usage.

```bash
# logue com a primeira conta no Codex CLI, depois:
python ccx_codex.py add

# codex logout, codex login com a segunda conta, depois:
python ccx_codex.py add

# confira
python ccx_codex.py status

# veja Claude Code e Codex na mesma saída
python ccx.py stats

# monitor automático (precisa de 2+ contas)
python ccx_codex.py auto
```

Os comandos, flags e a leitura do `status`/`auto` são idênticos aos do
`ccx.py` (ver [Comandos](#comandos) e [Estratégias de troca](#estratégias-de-troca));
só troque `ccx.py` por `ccx_codex.py`. `ccx-codex-auto.cmd` faz o mesmo que
`ccx-auto.cmd`, para o Codex.

### O que é diferente do módulo Claude

- **Um arquivo só.** O Codex CLI guarda tudo em `~/.codex/auth.json`
  (`CODEX_HOME` para outro caminho), sem o equivalente ao `.claude.json`
  separado. A troca continua cirúrgica: só reescreve `tokens` e
  `last_refresh`, preservando `auth_mode`/`OPENAI_API_KEY` como estavam.
- **Token é JWT.** `access_token` e `id_token` são JWTs de verdade. A
  expiração vem do claim `exp` do `access_token` (decodificado localmente só
  para leitura, sem checar assinatura), e a identidade (e-mail, `account_id`,
  `workspace_id`) vem dos claims do `id_token`. Não existe um campo
  `expiresAt` gravado à parte como no `.credentials.json` do Claude Code.
- **Refresh:** `POST https://auth.openai.com/oauth/token` com
  `grant_type=refresh_token` e o `client_id` público do Codex CLI
  (`app_EMoamEEZ73f0CkXaXp7hrann`). Erros permanentes (`invalid_grant`,
  `refresh_token_expired`, `token_invalidated` etc., o conjunto documentado
  pela própria API) marcam o slot como `MORTO`, igual ao módulo Claude.
- **Usage:** `GET https://chatgpt.com/backend-api/wham/usage`, com
  `chatgpt-account-id` no header. A resposta traz `primary_window` (janela
  curta, ~5h) e `secondary_window` (semanal), mapeados para os mesmos rótulos
  `5h`/`7d` que o resto do código já entende, então a engine de decisão do
  `ccx.py` funciona sem nenhuma alteração.
- **Sem lock cooperativo confirmado com o Codex CLI real.** O módulo Claude
  segura o lock de diretório que o próprio Claude Code usa no refresh, lido
  do código dele. Para o Codex CLI não há confirmação equivalente: o
  [codex-lb](https://github.com/Soju06/codex-lb), projeto que inspirou este
  módulo, é um proxy de rede e nunca escreve `auth.json` local por baixo de
  um Codex CLI rodando, então não serviu de referência para esse ponto
  específico. A escrita continua atômica (arquivo temporário + `os.replace`),
  mas trocar no instante exato em que o Codex CLI está renovando o próprio
  token é uma janela de corrida que este módulo não cobre. Evite rodar
  `ccx_codex auto` enquanto usa o Codex CLI ativamente no mesmo segundo em
  que uma troca cair.
- **Workspace em vez de organização.** O aviso de cota compartilhada usa
  `workspace_id` (seats de Team/Enterprise) em vez do `organizationUuid` do
  Claude Code.
- **Reset banking não implementado.** Ver [Limitações](#limitações).

### Origem

Este módulo nasceu de ler o código-fonte do
[codex-lb](https://github.com/Soju06/codex-lb) (load balancer de contas
ChatGPT, proxy completo com dashboard) para extrair o protocolo real:
endpoint de usage, endpoint e client_id de refresh, e o formato do
`auth.json`. A arquitetura de proxy dele não foi portada, o `ccx_codex`
continua com a filosofia do `ccx.py`: sem servidor, sem dependência, só
reposiciona a credencial que o Codex CLI oficial usa.

### Teste

```bash
python test_ccx_codex.py
```

Mesmo estilo do `test_ccx.py`, cobrindo o que é específico do Codex: leitura
de claims do JWT, expiração via `exp`, classificação de erro de refresh
(permanente vs. transitório), mapeamento de `primary_window`/`secondary_window`
para o formato `5h`/`7d`, preservação de `auth_mode`/`OPENAI_API_KEY` na
troca, e identidade sobrevivendo à rotação de refresh token.

## Licença

[MIT](LICENSE).

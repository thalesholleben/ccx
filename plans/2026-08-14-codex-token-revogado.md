# Marcar como MORTO o slot Codex com token revogado no servidor

Status: CONCLUÍDO
Work ID: codex-token-revogado

## Objetivo

Fazer o `ccx_codex` reconhecer sozinho um slot cujo grant OAuth foi revogado no
servidor da OpenAI enquanto o `exp` do access token ainda está no futuro. Hoje esse
slot fica repetindo `HTTP 401` no `status` e no `auto` indefinidamente, sem nunca
receber a marca `dead` que o próprio módulo já sabe exibir e respeitar.

## Contexto e evidências inspecionadas

Diagnóstico feito em 2026-08-14 com duas contas reais em estado revogado.

Causa externa, já confirmada e documentada no `README.md`: `codex logout` faz
`POST https://auth.openai.com/oauth/revoke` antes de apagar o `auth.json`. As strings
`failed to revoke auth tokens during logout` e
`CODEX_REVOKE_TOKEN_URL_OVERRIDE ... https://auth.openai.com/oauth/revoke` estão no
binário do Codex CLI 0.144.1. Isso invalida o grant inteiro daquela conta, inclusive a
cópia guardada no slot do CCX.

Respostas reais capturadas dos dois endpoints, com o mesmo token:

- `GET https://chatgpt.com/backend-api/wham/usage` devolveu `401` com corpo
  `{"error":{"message":"Encountered invalidated oauth token for user, failing request","code":"token_revoked"},"status":401}`
- `POST https://auth.openai.com/oauth/token` devolveu `401` com corpo
  `{"error":{"message":"Your session has ended. Please log in again.","code":"refresh_token_invalidated"}}`

O ponto que importa para o defeito: nesses dois slots o `exp` do access token ainda
estava a 10 e a 9 dias de distância. O token era inválido no servidor e válido no papel.

**Evidência que mudou o desenho, colhida às 18:55 do mesmo dia.** Depois de várias
leituras seguidas do endpoint de usage, `ccx_codex status` devolveu `HTTP 401` para os
**quatro** slots ao mesmo tempo, incluindo dois que tinham respondido `200` treze minutos
antes. Uma sonda direta poucos segundos depois devolveu `200` nos quatro
(`primary_window` 100, 100, 88 e 0). Ou seja: **o endpoint de usage devolve `401`
transitório**, provavelmente como resposta a volume, e não só quando o grant morreu. A
primeira versão deste plano assumia o contrário ("se o token fosse bom, não haveria
401") e teria disparado refresh nas quatro contas por causa desse blip, rotacionando
quatro refresh tokens à toa. Rotação desnecessária não é inócua: `refresh_token_reused`
está em `DEAD_REFRESH_CODES` justamente porque token rotacionado por baixo de outro
processo é um jeito conhecido de matar a conta.

**Segundo achado da revisão cruzada, confirmado localmente: `refresh_tokens` não
reconhece o payload real.** `ccx_codex.py:183-186` só lê `error` quando ele é string, ou
`code` na raiz do JSON. A resposta real da OpenAI traz o código **aninhado**, em
`error.code`. Reprodução executada com `ccx.http_json` stubado para levantar `HTTPError`
401 com o corpo capturado hoje:

```
payload real aninhado    -> (None, 'transient')
payload plano invalid_grant -> (None, 'dead')
```

Consequência: hoje, mesmo quando o `exp` vence e o refresh finalmente roda, um grant
revogado é classificado como falha passageira e o slot nunca morre. O defeito é maior do
que a task 194 descrevia, e sem corrigir isso o critério 1 deste plano é inalcançável.

Código inspecionado:

- `ccx_codex.py:325-345` (`slot_usage`): o refresh, único caminho que classifica um
  token como `dead`, só é tentado quando `token_expired(tokens)` é verdadeiro. O `401`
  vindo de `fetch_usage` é convertido em `f"HTTP {e.code}"` e devolvido como erro
  transitório qualquer.
- `ccx_codex.py:338-339`: a marca `dead`, quando existe, já produz a mensagem correta
  (`token morto: relogue e rode 'ccx_codex add'`) e já tira o slot da decisão.
- `ccx_codex.py:74-87` (`DEAD_REFRESH_CODES`): `refresh_token_invalidated` já está no
  conjunto de códigos permanentes, então `refresh_tokens` já devolve `"dead"` para este
  caso. Não é preciso mexer na classificação, só chegar até ela.
- `ccx.py:526-570` (`cached_slot_usage`, `remember_slot_usage`): erro é cacheado por
  `USAGE_ERROR_CACHE_TTL_S = 120s`, e uma leitura boa recente sobrevive a um erro por
  `USAGE_STALE_DECISION_TTL_S = 300s`. Um slot morto devolve `usage=None` com erro, então
  entra nesse mesmo caminho.
- `ccx.py:674-706` (`slot_usage` do módulo Claude): tem exatamente a mesma estrutura e,
  portanto, o mesmo defeito em tese. Ver `Fora de escopo`.

Instruções lidas antes de fechar o plano: `README.md` (seções "Como funciona por dentro",
"Regras de refresh de token" e "Problemas conhecidos"), `AGENTS.md` e `CLAUDE.md` do
repositório, `CLAUDE.md` da raiz do workspace e a skill `ponytail`.

## Escopo

- `ccx_codex.py`, `refresh_tokens`: classificar também o código aninhado em `error.code`,
  preservando os dois formatos já aceitos hoje. É pré-requisito do resto.
- `ccx_codex.py`, `slot_usage`: ao marcar um slot como `dead`, invalidar também
  `usage_cache[key]`, **nos dois caminhos**, o novo e o que já existe por `exp` vencido.
- `ccx_codex.py`, `slot_usage`: tratar `401` **com `code: "token_revoked"`** como possível
  revogação e resolver a dúvida com uma única tentativa de refresh. Todo outro `401` fica
  como está.
- `test_ccx_codex.py`: cobrir o comportamento novo, incluindo os casos em que ele **não**
  deve disparar.
- `README.md`: substituir o parágrafo que hoje descreve o defeito como limitação conhecida.

## Fora de escopo

- **O mesmo ajuste em `ccx.py` (módulo Claude).** A estrutura é idêntica, mas não existe
  evidência capturada de que a API da Anthropic devolva `401` para token revogado com
  `exp` válido, e o gatilho externo (`codex logout`) não tem equivalente confirmado do
  lado Claude. Replicar por simetria seria escrever código para um caso não observado.
  Fica registrado aqui para quando houver evidência.
- Redimir `rate_limit_reset_credits`, limites semanais por modelo e qualquer outra
  limitação já listada no `README.md`.
- Impedir a revogação em si. Isso é comportamento do Codex CLI e já está documentado no
  `README.md` com o fluxo de cadastro que não usa `codex logout`.

## Requisitos e critérios de aceite

-1. **Slot marcado como `dead` sai da decisão na mesma coleta.** Ao marcar, a entrada
   correspondente em `usage_cache` é descartada, para que `remember_slot_usage` não
   ressuscite a última leitura boa e `pick_target` não possa eleger a conta morta. Vale
   também para o caminho de `dead` que já existe hoje por `exp` vencido.
0. **`refresh_tokens` classifica como `dead` o payload real
   `{"error":{"code":"refresh_token_invalidated"}}` em `401`.** Os formatos que já
   funcionam hoje (`{"error":"invalid_grant"}` e `code` na raiz) continuam funcionando.
   Sem este item, nenhum dos seguintes é alcançável.
1. Slot inativo, `exp` no futuro, usage devolve `401` **com `code: "token_revoked"`**,
   refresh devolve código permanente: o slot recebe `dead: true`, é persistido, e o erro
   passa a ser `token morto: relogue e rode 'ccx_codex add'`.
2. Slot inativo, mesmo `401` com `token_revoked`, mas o refresh **funciona**: os tokens
   novos são salvos, o usage é tentado de novo uma vez e a cota volta normalmente. O slot
   não é marcado morto.
2b. **Slot inativo, `401` com qualquer outro código, sem código ou com corpo ilegível:
   `refresh_tokens` não é chamado.** O erro continua `HTTP 401`, igual a hoje. Este é o
   critério que protege as contas saudáveis do `401` transitório observado nas 18:55.
3. Slot inativo, `401` com `token_revoked`, refresh falha de forma transitória: nada é marcado
   morto e o erro continua legível. A nova tentativa acontece na primeira coleta depois
   que o erro sai do `usage_cache`, ou seja, após `USAGE_ERROR_CACHE_TTL_S` (120s), e
   **não** necessariamente na checagem seguinte. Esse é o comportamento que o cache
   compartilhado já impõe a todo erro, e ele fica preservado de propósito.
4. **Slot ativo com `401` nunca dispara refresh.** A conta ativa é do Codex CLI; renovar
   por baixo dele é a regra que o `README.md` e o `AGENTS.md` proíbem.
5. No máximo **um** refresh por chamada de `slot_usage`. Se o refresh já ocorreu no topo
   da função por `exp` vencido e o usage ainda deu `401`, não há segunda tentativa.
6. Erro que não seja `401` (`429`, `500`, timeout) mantém exatamente o comportamento de
   hoje.
7. `python test_ccx.py`, `python test_ccx_codex.py` e `python test_ccx_profile.py` passam.

## Decisões e premissas

- **Disparar pelo código `token_revoked` no corpo, não pelo `401` sozinho.** Esta decisão
  é o inverso da primeira versão do plano e a evidência das 18:55 é o motivo: `401` puro
  não distingue grant morto de blip do endpoint, e tratar os dois igual faz o CCX
  rotacionar token de conta saudável. O corpo `{"error":{"code":"token_revoked"}}` é o
  servidor afirmando que o token foi invalidado, que é exatamente a condição que queremos
  detectar.
- **`401` com qualquer outro código, ou sem código legível, mantém o comportamento de
  hoje.** Se a beta do endpoint trocar a string, o CCX degrada para o loop de `HTTP 401`
  atual, que é ruim mas conhecido, em vez de degradar para rotação indevida de token. O
  modo de falha escolhido é o inerte.
- **Quem dá o veredito final continua sendo o refresh.** O `token_revoked` só autoriza
  *perguntar*; é o `POST /oauth/token` que classifica como `dead`, pelo conjunto
  `DEAD_REFRESH_CODES` já existente. Um `token_revoked` isolado nunca mata o slot sozinho.
- **Não introduzir helper compartilhado em `ccx.py` agora.** O `AGENTS.md` manda mover
  para `ccx.py` o que for genérico, mas os dois `slot_usage` têm formatos de credencial
  diferentes e o módulo Claude está fora de escopo por falta de evidência. Criar a
  abstração para um único usuário é over-engineering.
- Premissa: `refresh_tokens` já classifica `refresh_token_invalidated` como `"dead"`.
  Confirmado em `ccx_codex.py:74-87` e coberto por `test_refresh_tokens_classifica_erro`.
- **Não abrir exceção no `usage_cache` para o `401` transitório.** Levantado pela revisão
  cruzada (CR1-P1-001), com reprodução: duas chamadas seguidas a `collect` com `slot_usage`
  devolvendo `HTTP 401` executam `slot_usage` uma única vez, porque `ccx.py:536-543` guarda
  qualquer erro por `USAGE_ERROR_CACHE_TTL_S` e `ccx_codex.py:358-364` consulta o cache
  antes. O critério 3 foi corrigido para descrever isso em vez de prometer o contrário.
  Manter o cache é deliberado: esse TTL existe para não transformar erro em tempestade de
  requisição, vale hoje para `429`, `500` e timeout, e um slot revogado de verdade nem
  chega nesse ramo, porque morre na primeira passada. O atraso máximo é de 120s numa conta
  que já está com a leitura quebrada.

## Mudanças propostas por arquivo ou componente

### `ccx_codex.py`, função `slot_usage`

Introduzir uma variável local que registre se o refresh já foi consumido nesta chamada, e
um segundo braço para o `401`. Efeito esperado: o slot revogado sai da rotação na primeira
checagem em vez de nunca.

Forma pretendida, mantendo o corpo atual intacto no caminho feliz:

```python
def _marca_morto(key, slot, store):
    """Marca e tira da decisao na mesma coleta. Descartar o usage_cache e o que
    impede remember_slot_usage de reaproveitar a ultima leitura boa por 300s e
    pick_target de eleger uma conta que ja sabemos morta."""
    slot["dead"] = True
    store.get("usage_cache", {}).pop(key, None)
    ccx.write_json(STORE, store)


def slot_usage(key, slot, is_active, store):
    tokens = slot["tokens"]
    ja_renovou = False
    if not is_active and token_expired(tokens):
        # ... bloco atual, sem alteracao ...
        ja_renovou = True
    if slot.get("dead"):
        return None, "token morto: relogue e rode 'ccx_codex add'"
    usage, erro, codigo = _tenta_usage(tokens, slot)
    if codigo != "token_revoked" or is_active or ja_renovou:
        return usage, erro
    # O servidor afirmou que este token foi invalidado, e o exp ainda nao
    # passou, entao o caminho normal de refresh nunca rodaria. Um refresh
    # aqui e o unico jeito de classificar o slot como morto. 401 sem esse
    # codigo NAO entra aqui: o endpoint devolve 401 transitorio, e rotacionar
    # token de conta saudavel e como se chega em refresh_token_reused.
    novos, err = refresh_tokens(tokens)
    if err == "dead":
        _marca_morto(key, slot, store)
        return None, "token morto: relogue e rode 'ccx_codex add'"
    if not novos:
        return None, "HTTP 401"
    slot["tokens"] = novos
    ccx.write_json(STORE, store)
    usage, erro, _ = _tenta_usage(novos, slot)
    return usage, erro
```

**Invariante que a revisão cruzada pegou (CR1-P1-002):** `slot_usage` devolve exatamente
dois elementos em todos os ramos. `collect` faz `usage_map[key], err_map[key] = cached` em
`ccx_codex.py:361-362` e repassa com `*cached` para `remember_slot_usage`; um terceiro
elemento vira `ValueError: too many values to unpack`. O terceiro valor de `_tenta_usage`
morre dentro de `slot_usage` e nunca escapa.

Com `_tenta_usage(tokens, slot)` sendo o `try/except` que hoje já existe no fim da função,
acrescido de um terceiro elemento no retorno: o código de erro que a OpenAI põe no corpo.
Os dois primeiros elementos mantêm exatamente os valores atuais.

```python
def _tenta_usage(tokens, slot):
    """(cota, erro, codigo do corpo). O codigo e "" quando nao ha corpo legivel."""
    try:
        return fetch_usage(tokens["access_token"], slot.get("account_id", "")), "", ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}", _codigo_do_erro(e)
    except Exception as e:
        return None, type(e).__name__, ""


def _codigo_do_erro(e) -> str:
    """error.code do corpo, ou "" se nao der para ler. Nunca levanta: isto roda
    dentro do tratamento de um erro e nao pode virar uma segunda falha."""
    try:
        return json.loads(e.read().decode(errors="replace"))["error"]["code"] or ""
    except Exception:
        return ""
```

`ccx.http_json` (`ccx.py:422-424`) levanta em `urlopen`, antes de tocar no corpo, então o
stream do `HTTPError` chega intacto e `e.read()` funciona. Confirmado na prática: as
respostas `token_revoked` e `refresh_token_invalidated` citadas acima foram lidas assim.

### `test_ccx_codex.py`

Onze testes novos, no estilo do arquivo (stub de `fetch_usage` e `refresh_tokens`,
`assert`, sem framework), um por critério de aceite:

0. `test_refresh_tokens_classifica_codigo_aninhado` (critério 0): o corpo real
   `{"error":{"code":"refresh_token_invalidated"}}` em `401` tem de virar `dead`, e os dois
   formatos que o `test_refresh_tokens_classifica_erro` já cobre continuam iguais
0a. `test_slot_morto_nao_e_eleito_pelo_cache` (critério -1): coleta que marca o slot como
   morto tendo leitura boa recente no cache; depois `pick_target` não pode devolvê-lo.
   Reproduz o cenário do bloqueador CR1-P1-001 da rodada anterior
0b. `test_slot_usage_devolve_sempre_dois_elementos` (CR1-P1-002): percorre os ramos de
   sucesso, de `401` recuperado, de morto e de erro comum conferindo `len(...) == 2`
1. `test_slot_usage_marca_morto_quando_401_e_refresh_permanente` (critério 1)
2. `test_slot_usage_recupera_quando_401_e_refresh_funciona` (critério 2)
2b. `test_slot_usage_ignora_401_sem_codigo_token_revoked` (critério 2b): `401` com
   `code` diferente, `401` sem `error.code` e `401` com corpo que não é JSON. Nos três,
   `refresh_tokens` não pode ser chamado e o erro tem de continuar `HTTP 401`. É o teste
   que trava a regressão que a evidência das 18:55 revelou
3. `test_slot_usage_nao_marca_morto_em_401_com_refresh_transitorio` (critério 3, parte a):
   o slot continua sem `dead` e o erro volta legível
4. `test_collect_nao_repete_usage_dentro_do_ttl_de_erro` (critério 3, parte b): duas
   coletas seguidas com `401`, contando as chamadas a `slot_usage`. Fixa a regra do cache
   de 120s em teste, que é a exigência do bloqueador CR1-P1-001
5. `test_slot_usage_nao_renova_conta_ativa_em_401` (critério 4)
6. `test_slot_usage_nao_renova_duas_vezes_na_mesma_chamada` (critério 5)
7. `test_slot_usage_nao_renova_em_erro_que_nao_e_401` (critério 6): `429`, `500` e uma
   exceção que não é `HTTPError` não podem chamar `refresh_tokens`

Atualizar a contagem de testes citada no `README.md` e no `AGENTS.md`: o módulo Codex sai
de 19 para 30.

### `README.md`

O parágrafo final do bloco "`codex logout` mata a conta no servidor" hoje descreve o
defeito como limitação vigente. Trocar pela descrição do comportamento novo: o slot
revogado é detectado e marcado morto na primeira checagem, com uma única tentativa de
refresh, e a conta ativa nunca é renovada.

## Sequência de execução

1. Corrigir `refresh_tokens` para ler `error.code` aninhado (critério 0) e escrever o teste
   dele. É pré-requisito: sem isso o slot revogado nunca vira `dead`.
2. Extrair `_tenta_usage` e `_codigo_do_erro`, com o terceiro elemento do retorno ainda
   sem consumidor. Rodar `test_ccx_codex.py`; tem de continuar verde.
3. Trocar as duas marcações de `dead` por `_marca_morto`, incluindo a que já existe.
4. Adicionar o braço do `token_revoked` com as guardas `is_active` e `ja_renovou`,
   respeitando o contrato de dois elementos em todos os ramos.
5. Escrever os testes restantes e vê-los passar.
6. Rodar as três suítes.
7. Atualizar `README.md` e as contagens em `README.md` e `AGENTS.md`.
8. `/cross-review` do diff, conforme a regra do workspace para mudança em credencial.
9. Fechar a task 194 no `Backlog.html`.

## Validação e testes

- `python test_ccx.py`, `python test_ccx_codex.py`, `python test_ccx_profile.py`.
- Validação real disponível sem custo: as duas contas revogadas de hoje foram marcadas
  mortas à mão. Basta remover a marca `dead` de um slot no store e rodar
  `python ccx_codex.py status`; o esperado é que ele volte a `token morto` sozinho, em vez
  de `HTTP 401`. É leitura mais um refresh contra um grant que já está morto, então não há
  efeito colateral em conta viva.
- Conferir que os slots saudáveis continuam devolvendo cota no mesmo `status`.

## Riscos, mitigação e rollback

- **Risco: renovar a conta ativa por engano e derrubar a sessão do Codex CLI.** É o risco
  sério aqui. Mitigado pela guarda `is_active` e por um teste dedicado (critério 4).
- **Risco: laço de refresh queimando rotações de token.** Mitigado pela guarda
  `ja_renovou` e pelo fato de o segundo `_tenta_usage` não ter terceira tentativa.
- **Risco: rotacionar token de conta saudável por causa de `401` transitório.** Este é o
  risco que a primeira versão do plano não via, e ele foi observado de verdade às 18:55 nas
  quatro contas. Mitigado por exigir `code: "token_revoked"`, pela guarda `is_active` e
  pelo teste 2b, que cobre os três formatos de `401` que não devem disparar nada.
- **Risco: marcar morto um token bom por causa de um `401` atípico do endpoint.** O slot
  só morre se o **refresh** devolver um código permanente. Um `401` de usage sozinho, mesmo
  com `token_revoked`, nunca mata o slot. E `ccx_codex add` limpa a marca `dead`.
- **Risco: a OpenAI trocar a string `token_revoked` e o defeito voltar em silêncio.**
  Aceito conscientemente. A degradação é para o comportamento de hoje, não para algo pior,
  e o `README.md` passa a registrar de qual código o CCX depende.
- Rollback: a mudança é local a uma função e aos testes. `git revert` do commit resolve,
  sem estado migrado nem formato de arquivo alterado.

## Dúvidas ou bloqueios

Nenhum bloqueio. Uma pergunta em aberto, deliberadamente resolvida como "não fazer agora":
replicar o ajuste em `ccx.py`. Ver `Fora de escopo`.

## Resultado da execução

Implementado em 2026-08-14, na ordem planejada.

- `ccx_codex.py`: `refresh_tokens` passou a ler `error.code` aninhado; novos
  `_error_code`, `_try_usage` e `mark_dead`; `slot_usage` ganhou o braço do
  `token_revoked` com as três guardas.
- `test_ccx_codex.py`: 19 para 30 testes.
- `README.md` e `AGENTS.md`: parágrafo do defeito trocado pela descrição do comportamento
  novo, contagens atualizadas.

Testes: `test_ccx.py` 42/42, `test_ccx_codex.py` 30/30, `test_ccx_profile.py` 4/4.
`python ccx.py stats` continua lendo as quatro contas normalmente.

**Validação não executada, e por quê:** o plano previa remover a marca `dead` de um slot
realmente revogado e ver o CCX remarcá-lo sozinho. Entre o planejamento e a execução as
duas contas foram recuperadas por relogin, então não existe mais grant revogado vivo para
o teste ponta a ponta. O caminho fica coberto pelos testes, que usam os corpos reais
capturados hoje (`token_revoked` no usage e `refresh_token_invalidated` no refresh).

## Revisão cruzada

### Replan rodada 3, revisor Codex, 2026-08-14: APROVADO

Parecer em `.git/cross-review/codex-token-revogado-replan-3/round-1.json`. Gate fechado;
execução liberada. Resumo do revisor: a classificação aninhada em `ccx_codex.py:177-187`, o
refresh limitado a `401`/`token_revoked` em `ccx_codex.py:325-345` e a eliminação da
reeleição por cache conforme `ccx.py:546-570` cobrem o problema, com as suítes atuais verdes.

### Replan rodada 2, revisor Codex, 2026-08-14: BLOQUEADO

Parecer em `.git/cross-review/codex-token-revogado-replan-2/round-1.json`.

- **CR1-P1-001 (P1)**: marcar o slot como morto não bastava. `remember_slot_usage`
  (`ccx.py:557-565`) preserva a última leitura boa por 300s quando o resultado novo é erro,
  e `pick_target` não conhece `dead`, então a conta morta continuava elegível por até cinco
  minutos. O revisor reproduziu: `pick_target= revogado`. **Resolvido no plano** com
  `_marca_morto`, que descarta `usage_cache[key]` junto, aplicado também ao caminho de
  `dead` que já existia. Virou o critério -1 e o teste 0a.

### Replan rodada 1, revisor Codex, 2026-08-14: BLOQUEADO

Parecer em `.git/cross-review/codex-token-revogado-replan-1/round-1.json`. Os dois
bloqueadores foram conferidos no artefato real antes de aceitos.

- **CR1-P1-001 (P1)**: `refresh_tokens` não classifica o código aninhado, então o payload
  real de revogação volta como `transient` e o slot jamais morre. **Reproduzido localmente**
  com `ccx.http_json` stubado. Vira o critério 0 e o passo 1 da sequência, e amplia o escopo
  para incluir `refresh_tokens`.
- **CR1-P1-002 (P1)**: o ramo de recuperação devolvia três elementos onde `collect`
  desempacota dois. Corrigido no esboço e transformado em invariante explícita com teste
  próprio.

### Rodada 1, revisor Codex, 2026-08-14: BLOQUEADO

Parecer em `.git/cross-review/codex-token-revogado/round-1.json`.

- **CR1-P1-001 (P1, bloqueador)**: o critério 3 prometia nova tentativa "na próxima
  checagem", o que o `usage_cache` compartilhado impede por 120s. O revisor reproduziu com
  duas chamadas seguidas a `collect`, obtendo `slot_usage_calls= 1`.
  **Resolvido no plano**: critério 3 reescrito para descrever o TTL real, decisão explícita
  de não abrir exceção de cache para esse caso, e teste 4 novo passando por `collect` para
  fixar a regra. Nenhuma mudança de comportamento pretendido; o que estava errado era a
  descrição do aceite.
- **CR1-P2-001 (P2, ressalva)**: a bateria proposta cobria só os critérios 1, 2, 4 e 5.
  **Resolvido no plano**: a lista foi de quatro para oito testes, um por critério, incluindo
  o refresh transitório e os erros não-`401`.

### Rodada 2, revisor Codex, 2026-08-14: FIX_NAO_CONFIRMADO (descasamento de protocolo)

Parecer em `.git/cross-review/codex-token-revogado/round-2.json`. O revisor procurou o
conserto no código (`git diff` vazio, 19 testes em vez dos previstos) e concluiu
`NAO_CORRIGIDO`. Está correto do ponto de vista dele: a rodada 2 verifica fix implementado.
Só que o bloqueador era de plano e o conserto de um plano é o próprio plano editado, que a
rodada 2 não avalia. O gate permaneceu fechado e nada foi implementado.

### Replanejamento, 2026-08-14

Depois da rodada 2, uma observação nova invalidou uma premissa central: o endpoint de usage
devolveu `401` transitório nas quatro contas de uma vez (detalhe em `Contexto e
evidências`). O desenho mudou de "todo `401` autoriza um refresh" para "só `401` com
`code: token_revoked` autoriza". Isso é mudança material de risco e de aceite, então o plano
segue para uma rodada 1 nova sob o Work ID `codex-token-revogado-replan-1`, em vez de
insistir na rodada 2 do ciclo anterior.

### Implementação, revisor Codex, 2026-08-14

Work ID `codex-token-revogado-impl`, `--kind code --scope working`.

**Rodada 1: BLOQUEADO.** CR1-P1-001 (P1): a guarda olhava só `error.code`, sem o status.
Um `HTTP 500` com `token_revoked` no corpo disparava refresh de conta inativa, violando o
critério 6. Reprodução do revisor: `slot_usage= (None, 'HTTP 500')` com `refresh_calls= 1`.
**Corrigido** exigindo `error == "HTTP 401"` na condição, com o caso somado ao
`test_slot_usage_nao_renova_em_erro_que_nao_e_401`.

**Rodada 2: FIX_CONFIRMADO.** O revisor confirmou com sondas próprias de `HTTP 500` e
`HTTP 503`, ambas com `refresh_calls= 0`, e as três suítes verdes. O runner recusou o raw
por coerência de schema (o revisor deixou `blockers` vazio em vez de listar CR1-P1-001 como
`CORRIGIDO`), e o parecer foi anexado com `record --from-raw`, que é o registro manual
previsto no protocolo para raw estruturalmente válido recusado pela validação semântica.
Mérito não foi alterado.

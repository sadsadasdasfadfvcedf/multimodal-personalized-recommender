"""
src/text_norm.py
================

中文文本规范化与流畅度检测（服务于真实 LLM 解释）。

解决两个实测问题：
    1. **繁体字**：Qwen3-4B 会输出「這10個復古彩球聖誕燈，裝飾品選項，價格實惠…」；
    2. **中英混排**：同一句里会漏出英文词（如 ball）。

设计原则（与项目其余部分一致）：**可选依赖 + 安全降级**
    - 装有 `zhconv` 时：自动繁转简，并替换常见英文词，归一化后再过门控；
    - 未装 `zhconv` 时：不做转换，但**检测**到繁体字/残留英文即判定流畅度不合格，
      由上层回退模板解释 —— 宁可回退，也不把不规范文本展示给用户。

内置繁体字表（TRADITIONAL_MARKERS）由 zhconv 的权威繁简词表推导：
取所有「单字繁简不同、且该字不是任何字的简体形式」的 CJK 统一汉字，共 4056 字。
推导脚本见 scripts/12_text_norm_check.py 的注释说明。
"""

from __future__ import annotations

import re

from config import settings

NL = chr(10)

# 繁体专有字表（zhconv zh-cn 词表推导，仅保留 CJK 统一汉字）
TRADITIONAL_MARKERS = frozenset(
    "丟丣並乗乹乾亁亂亙亝亞亱亷亾佇佈佔併來侖侶侷俁係俓俔俠俥俬俻倀倆倈倉個倐們倖倣倫倲倸偉偑側偵偽傌傑傖傘備傚傢傭傯傳傴債傷傾僂僅僉僊働僑僕僞僤僥僨僱價儀儁儂億儈儉儌儎儐儔儕儘償儣優儭儲儷儸儺儻儼兇兌兎兒"
    "兗兠內兩冄冊冐冑冪冺凈凍凙凜凢凱凴別刦刧刪刼剄則剉剋剎剏剗剙剛剝剮剳剴創剷剹剾劃劄劇劉劊劌劍劏劑劒劚効勁勅勌勑動務勛勝勞勢勣勦勩勱勳勵勸勻匟匭匯匱匲匳區協卹卻卽厀厙厠厤厭厰厲厴參叄叢吒吚吳吶呂呌呪咊咼"
    "員哯哶唄唓唕唘唚唸啎問啑啓啗啞啟啢啣喎喚喪喫喬單喲嗁嗆嗇嗊嗎嗚嗩嗰嗶嗹嘆嘍嘑嘓嘔嘖嘗嘜嘠嘩嘪嘮嘯嘰嘳嘵嘷嘸嘺嘽噁噅噉噓噚噝噞噠噥噦噯噲噴噸噹嚀嚇嚌嚐嚕嚙嚛嚥嚦嚧嚨嚮嚲嚳嚴嚶嚽囀囁囂囃囅囈囉囌囑囒囓囙囪"
    "圅圇國圍園圓圖團圞坿垜垻埡埨埬埰執堅堊堖堚堝堯報場塊塋塏塒塗塚塟塢塤塲塵塸塹塿墊墖墜墠墮墰墲墳墶墻墾壇壈壋壎壓壗壘壙壚壜壞壟壠壢壣壩壪壯壺壻壼壽夘夠夢夥夾奐奧奩奪奬奮奼妝妬妳妷姉姍姙姦姪姸娙娛婁婡婣婦"
    "婬婭媈媍媧媯媰媼媽媿嫋嫗嫰嫵嫺嫻嫿嬀嬃嬇嬈嬋嬌嬙嬝嬡嬣嬤嬦嬪嬭嬰嬸嬻嬾孃孄孆孇孋孌孎孫學孻孼孾孿宂宮寀寃寑寠寢實寧審寫寬寳寵寶尅將專尋對導尒尙尟尠尷屆屍屓屛屜屢層屨屩屬屭岅岡峝峯峴島峽崍崑崗崙崢崬嵐嵗"
    "嵼嵽嵾嶁嶃嶄嶇嶈嶔嶗嶘嶠嶢嶧嶨嶮嶸嶹嶺嶼嶽巊巋巒巔巖巗巘巠巰巵巹帀帋帥師帬帳帶幀幃幇幑幓幗幘幙幚幝幟幣幩幫幬幹幾庫庻庽廁廂廄廈廎廐廕廚廝廞廟廠廡廢廣廧廩廬廳廵廹廻弒弔弳張強彃彄彆彈彌彎彔彙彞彠彥彫彲彿"
    "徃後徑從徠徧復徵徹徿怱怳恆恠恡恥悅悞悤悵悶悽惏惡惥惱惲惷惻愛愜愨愴愷愻愽愾慄態慍慘慙慚慟慣慤慪慫慮慳慴慶慺慼慽慾憂憇憊憍憐憑憒憖憚憢憤憫憮憲憶憸憹懀懇應懌懍懎懞懟懣懤懧懨懲懶懷懸懺懼懾戀戇戔戞戧戩戰戱"
    "戲戶戹扞抝拋拚挩挱挵挾捄捨捫捱捲掃掄掆掗掙掚掛採掽揀揑揚換揫揮揯揷揹搆搇搉損搖搗搤搥搨搯搵搶搾摃摋摐摑摜摟摯摳摶摺摻撈撊撏撐撓撝撟撡撣撥撦撧撫撲撳撻撾撿擁擄擇擊擋擓擔擕據擟擠擣擥擧擫擬擯擰擱擲擴擷擺擻"
    "擼擽擾攄攆攋攏攔攖攙攛攜攝攢攣攤攩攪攬攷敂敍敎敓敗敘敵數敺斂斃斅斆斕斬斷斸於旂旣旤旹旾昇昬時晉晛晝暈暉暐暘暢暫暱曄曆曇曉曊曏曖曠曡曥曨曬書會朞朢朥朧朮朶東杴枱枴柵柺査栁栞栢栰桒桮桱桺桿梔梖梘梜條梟梲棄"
    "棊棖棗棟棡棧棲棶椏椗椲椶椷椾楇楊楓楥楨業極榘榦榪榮榲榿構槍槓槕槤槧槨槫槮槳槶槼樁樂樅樑樓標樞樠樢樣樤樧樫樳樸樹樺樿橈橋橜機橢橫橯檁檉檔檜檟檢檣檭檮檯檳檵檸檻櫃櫅櫈櫍櫓櫚櫛櫝櫞櫟櫠櫥櫧櫨櫪櫫櫬櫱櫳櫸櫻欄"
    "欅欇權欍欏欐欑欒欓欖欘欝欞欵欽歎歐歛歟歡歲歴歷歸歿殀殘殞殢殤殨殫殭殮殯殰殲殺殻殼毀毆毊毧毬毿氂氈氊氌氣氫氬氭氳氷氾汎汙汚決沒沖況泝洩洶浹浿涇涖涗涼淒淚淛淥淨淩淪淵淶淺渖渙減渢渦測渾湊湋湞湧湯湼溈準溝溡"
    "溫溮溳溼滄滅滌滎滙滛滬滯滲滷滸滻滾滿漁漊漍漚漢漣漬漲漵漸漿潁潄潑潔潕潙潚潛潣潤潯潰潷潿澀澁澅澆澇澐澗澠澤澦澩澫澬澮澱澾濁濃濄濆濇濕濘濚濛濜濟濤濧濫濰濱濶濺濼濾濿瀂瀃瀅瀆瀇瀉瀋瀏瀕瀘瀝瀟瀠瀦瀧瀨瀰瀲瀾灃"
    "灄灋灍灑灒灕灘灙灝灠灡灣灤灧灩災為烏烖烴無煇煉煑煒煗煙煢煥煩煬煱熂熅熈熉熌熒熓熗熚熡熰熱熲熾燀燁燄燈燉燒燖燙燜營燦燬燭燴燶燻燼燾爃爄爇爍爐爖爗爛爥爧爭爲爺爾牀牆牋牎牐牓牕牘牠牴牽犖犛犞犢犧狀狥狹狽猂猌"
    "猙猨猶猻獁獃獄獅獊獎獘獧獨獩獪獫獮獰獱獲獵獷獸獺獻獼玀玁玅珼現琖琱琺琿瑇瑋瑒瑣瑤瑩瑪瑯瑲瑻瑽璉璊璕璗璝璡璢璣璦璫璯環璵璸璼璽璾璿瓄瓅瓊瓏瓔瓕瓚瓛甌甎甕甖甞產産甦甯畂畆畝畢畧畫畮異畱畵當畼疇疊疎疘疿痐痙"
    "痠痮痲痺痾瘂瘉瘋瘍瘓瘖瘞瘡瘧瘮瘱瘲瘺瘻療癄癅癆癇癈癉癐癒癘癟癡癢癤癥癧癩癬癭癮癰癱癲發皁皐皚皜皟皰皷皸皺盃盇盌盜盞盡監盤盧盨盪眎眝眞眡眥眾睍睏睜睞睠睪瞇瞖瞘瞜瞞瞤瞭瞶瞼矁矇矉矑矓矙矚矯矽砲硃硜硤硨硯碕"
    "碙碩碪碭碸確碼碽磑磚磟磠磣磧磯磽磾礄礆礎礐礒礙礦礪礫礬礮礱祕祘祿禍禎禕禡禦禪禮禰禱禿秈秊秌秖稅稈稉稏稜稟稬稭種稱稾穀穇穌積穎穠穡穢穤穨穩穫穭穽窓窩窪窮窯窰窵窶窺窻竄竅竇竈竊竚竝竢竪竱競筆筍筞筧筩筯筴箇"
    "箋箏箒箠節範築篋篔篘篛篠篢篤篩篳篸簀簂簍簑簒簞簡簢簣簫簮簷簹簽簾籃籅籋籌籐籔籙籛籜籟籠籤籩籪籬籮籲粃粧粵糉糝糞糧糰糲糴糶糹糺糾紀紂紃約紅紆紇紈紉紋納紐紓純紕紖紗紘紙級紛紜紝紞紟紡紥紬紮細紱紲紳紵紹紺紼"
    "紿絀絁終絃組絅絆絍絎絏結絕絙絛絝絞絡絢絥給絧絨絪絰統絲絳絶絹絺綀綁綃綄綆綇綈綉綋綌綎綏綐綑經綖綜綝綞綟綠綡綢綣綧綪綫綬維綯綰綱網綳綴綵綸綹綺綻綽綾綿緄緇緊緋緍緐緑緒緓緔緗緘緙線緜緝緞緟締緡緣緤緥緦編緩"
    "緬緮緯緰緱緲練緶緷緸緹緻緼縈縉縊縋縍縎縐縑縕縗縛縝縞縟縣縧縫縬縭縮縯縰縱縲縳縴縵縶縷縸縹縺總績繂繃繅繆繈繏繐繒繓織繕繖繙繚繞繟繡繢繦繨繩繪繫繬繭繮繯繰繳繶繷繸繹繻繼繽繾繿纁纆纇纈纊續纍纏纓纔纕纖纗纘纚"
    "纜缐缽罃罇罈罋罌罎罰罵罷罸羅羆羈羋羗羢羣羥羨義羵羶翄習翫翬翶翹翽耡耬耮聖聞聯聰聲聳聵聶職聹聻聽聾肅肎肐肧胷脃脅脇脈脗脛脣脥脩脫脹腁腎腖腡腦腪腫腳腸膃膓膕膚膞膠膢膩膹膽膾膿臈臉臋臍臏臕臗臘臙臚臝臟臠臢臥"
    "臨臯臺與興舉舊舖舘舩艙艢艣艤艦艪艫艱艷芲芻茘茲荊荳莊莖莢莧菕菫華菴菸萇萊萬萲萴萵葉葒葝葠葤葦葯葷蒍蒐蒓蒔蒕蒞蒭蒼蓀蓆蓋蓡蓧蓮蓯蓴蓽蔄蔔蔕蔘蔞蔣蔥蔦蔭蔯蔿蕁蕆蕎蕒蕓蕕蕘蕚蕝蕢蕩蕪蕭蕳蕷蕽蕿薀薆薈薊薌薑薔"
    "薘薟薦薩薳薴薵薹薺藍藎藝藥藪藭藴藶藷藹藺藼蘀蘄蘆蘇蘊蘋蘐蘓蘚蘞蘟蘢蘤蘭蘺蘿虆虉處虛虜號虧虯蚘蛕蛺蛻蜆蜋蜖蜨蝀蝕蝟蝦蝨蝯蝱蝸螄螎螞螡螢螮螻螿蟁蟂蟄蟇蟈蟎蟘蟜蟣蟬蟯蟲蟳蟶蟻蠀蠁蠅蠆蠍蠏蠐蠑蠒蠔蠙蠟蠣蠦蠨蠭"
    "蠱蠶蠻蠾衂衆衇衊術衕衚衛衝衞衺袞袟袵裊裌裏補裝裠裡製複褌褘褭褲褳褸褻襀襃襇襉襍襏襓襖襗襘襝襠襤襪襬襯襰襲襴襵覇覈覊見覎規覓覔視覘覛覡覥覦覩親覬覯覲覷覹覺覼覽覿觀觝觴觶觸訁訂訃計訊訌討訏訐訑訒訓訕訖託記"
    "訛訜訝訞訟訢訣訥訨訩訪設許訴訶診註証詀詁詆詊詎詐詑詒詓詔評詖詗詘詛詝詞詠詡詢詣試詩詪詫詬詭詮詰話該詳詵詶詷詼詿誂誄誅誆誇誋誌認誑誒誕誖誘誚語誠誡誣誤誥誦誨說誫説誰課誳誴誶誷誹誺誼誾調諂諄談諉請諍諏諑諒"
    "諓論諗諛諜諝諞諟諡諢諣諤諥諦諧諫諭諮諯諰諱諲諳諴諶諷諸諺諼諾謀謁謂謄謅謆謉謊謌謎謏謐謔謖謗謙謚講謝謠謡謨謫謬謭謯謱謳謸謹謾譁譂譅譆證譊譌譎譏譑譓譔譖識譙譚譜譞譟譨譫譭譯議譴護譸譼譽譾讀讁讅變讋讌讎讐讒"
    "讓讕讖讚讜讞讬谘豈豎豐豓豔豬豵豶貍貓貗貙貛貝貞貟負財貢貧貨販貪貫責貯貰貲貳貴貶買貸貺費貼貽貿賀賁賂賃賄賅資賈賉賊賑賒賓賕賙賚賛賜賝賞賟賠賡賢賣賤賦賧質賫賬賭賰賴賵賷賺賻購賽賾贃贄贅贇贈贉贊贋贍贏贐贑贓"
    "贔贖贗贚贛贜赬趂趕趙趨趲跡跥踁踐踫踰踴蹌蹏蹔蹕蹟蹠蹣蹤蹧蹳蹵蹺蹻躂躉躊躋躍躎躑躒躓躕躘躚躝躡躥躦躪躭躳躶軀軉車軋軌軍軏軑軒軔軕軗軛軜軝軟軤軨軫軬軲軷軸軹軺軻軼軾軿較輄輅輇輈載輊輋輒輓輔輕輖輗輙輛輜輝輞"
    "輟輢輥輦輨輩輪輬輭輮輯輳輶輷輸輻輼輾輿轀轂轄轅轆轇轉轊轍轎轐轔轗轟轠轡轢轣轤辠辢辤辦辭辮辯農辳迴迻逈逕這連逥逩逬週進遉遊運過達違遙遜遞遠遡適遯遱遲遶遷選遺遼邁還邇邊邏邐郟郵鄆鄉鄒鄔鄖鄟鄧鄩鄭鄰鄲鄳鄴鄶"
    "鄺酇酈酖酧醃醆醕醖醜醞醟醣醫醬醯醱醲醶醻醼釀釁釃釅釋釐釒釓釔釕釗釘釙釚針釟釣釤釦釧釨釩釬釲釳釴釵釷釹釺釾釿鈀鈁鈃鈄鈅鈆鈇鈈鈉鈋鈍鈎鈐鈑鈒鈔鈕鈖鈗鈛鈞鈠鈡鈣鈥鈦鈧鈮鈯鈰鈲鈳鈴鈷鈸鈹鈺鈽鈾鈿鉀鉁鉄鉅鉆鉈鉉"
    "鉊鉋鉍鉑鉔鉕鉗鉚鉛鉝鉞鉠鉢鉤鉥鉦鉧鉨鉬鉭鉮鉲鉳鉶鉷鉸鉺鉻鉽鉾鉿銀銁銂銃銅銈銊銍銏銑銓銖銘銚銛銜銠銣銥銦銨銩銪銫銬銱銲銳銶銷銹銻銼鋁鋂鋃鋅鋇鋉鋌鋏鋐鋒鋗鋙鋝鋟鋠鋣鋤鋥鋦鋨鋩鋪鋭鋮鋯鋰鋱鋶鋸鋹鋼錀錁錂錄錆"
    "錇錈錏錐錒錕錘錙錚錛錜錝錞錟錠錡錢錤錥錦錨錩錫錮錯録錳錶錸錼錽鍀鍁鍃鍄鍅鍆鍇鍈鍉鍊鍋鍍鍒鍔鍘鍚鍛鍠鍤鍥鍩鍫鍬鍭鍮鍰鍳鍵鍶鍺鍼鍾鎂鎄鎇鎈鎊鎌鎍鎓鎔鎖鎗鎘鎙鎚鎛鎝鎞鎡鎢鎣鎦鎧鎩鎪鎬鎭鎮鎯鎰鎲鎳鎵鎶鎷鎸鎻鎿"
    "鏃鏆鏇鏈鏉鏌鏍鏏鏐鏑鏗鏘鏚鏜鏝鏞鏟鏡鏢鏤鏥鏦鏨鏰鏵鏷鏹鏺鏻鏽鏾鐃鐄鐇鐈鐋鐍鐎鐏鐐鐒鐓鐔鐘鐙鐝鐠鐥鐦鐧鐨鐩鐪鐫鐮鐯鐲鐳鐵鐶鐸鐺鐼鐽鐿鑀鑄鑈鑉鑊鑌鑑鑒鑔鑕鑚鑛鑞鑠鑣鑤鑥鑪鑭鑰鑱鑲鑴鑵鑷鑹鑼鑽鑾鑿钁钂钜锺镟"
    "長門閂閃閆閈閉開閌閍閎閏閐閑閒間閔閗閘閙閝閞閡閣閤閥閧閨閩閫閬閭閱閲閵閶閹閻閼閽閾閿闃闆闇闈闉闊闋闌闍闐闑闒闓闔闕闖闚關闞闠闡闢闤闥阨阬陗陘陜陝陞陣陰陳陸陻陽陿隂隄隉隊階隑隕隖際隣隤隨險隮隯隱隴隷隸隻"
    "雋雖雙雛雜雞離難雲電霑霢霣霧霼霽靂靄靆靈靉靚靜靝靦靧靨靭靱鞀鞏鞝鞦鞵鞽鞾韁韃韆韈韉韋韌韍韓韙韚韛韜韝韞韠韤韮韻響頁頂頃項順頇須頊頌頍頎頏預頑頒頓頔頗領頜頟頠頡頤頦頫頭頮頰頲頴頵頷頸頹頻頼頽顂顃顅顆顇顋"
    "題額顎顏顒顓顔顗願顙顛類顢顣顥顦顧顫顬顯顰顱顳顴風颭颮颯颰颱颳颶颷颸颺颻颼颾飀飃飄飆飈飋飛飜飠飢飣飤飥飦飩飪飫飭飯飱飲飴飵飶飼飽飾飿餁餃餄餅餈餉養餌餎餏餑餒餓餔餕餖餗餘餚餛餜餞餡餦餧館餪餫餬餭餱餳餵餶"
    "餷餸餹餺餻餼餽餾餿饁饃饅饈饉饊饋饌饍饑饒饗饘饜饝饞饟饠饢馀馬馭馮馯馱馳馴馹馼駁駃駈駉駊駎駐駑駒駓駔駕駘駙駚駛駝駞駟駡駢駤駧駩駪駫駭駰駱駶駸駻駼駿騁騂騃騄騅騉騊騌騍騎騏騐騑騔騖騙騚騜騝騞騟騠騣騤騧騪騫騭"
    "騮騰騱騴騵騶騷騸騻騼騾驀驁驂驃驄驅驊驋驌驍驎驏驓驕驗驘驙驚驛驟驢驤驥驦驨驪驫骯骽骾髈髏髒體髕髖髥髮鬀鬆鬉鬍鬖鬚鬠鬢鬥鬦鬧鬨鬩鬪鬮鬰鬱鬹魎魘魚魛魟魢魥魦魨魯魴魵魷魺魽鮀鮁鮃鮄鮅鮆鮈鮊鮋鮍鮎鮐鮑鮒鮓鮚鮜鮝"
    "鮞鮟鮠鮡鮣鮤鮦鮪鮫鮭鮮鮯鮰鮳鮵鮶鮸鮺鮿鯀鯁鯄鯆鯇鯉鯊鯒鯔鯕鯖鯗鯛鯝鯞鯡鯢鯤鯧鯨鯪鯫鯬鯰鯱鯴鯶鯷鯻鯽鯾鯿鰁鰂鰃鰆鰈鰉鰊鰋鰌鰍鰏鰐鰑鰒鰓鰕鰛鰜鰟鰠鰣鰤鰥鰦鰧鰨鰩鰫鰭鰮鰱鰲鰳鰵鰶鰷鰹鰺鰻鰼鰽鰾鱀鱂鱄鱅鱆鱇鱈"
    "鱉鱊鱒鱔鱖鱗鱘鱚鱝鱟鱠鱢鱣鱤鱧鱨鱭鱮鱯鱲鱷鱸鱺鳥鳧鳩鳬鳲鳳鳴鳶鳷鳼鳽鳾鴀鴃鴅鴆鴇鴈鴉鴐鴒鴔鴕鴗鴛鴜鴝鴞鴟鴣鴥鴦鴨鴮鴯鴰鴲鴳鴴鴷鴻鴽鴿鵁鵂鵃鵊鵏鵐鵑鵒鵓鵚鵜鵝鵞鵟鵠鵡鵧鵩鵪鵫鵬鵮鵯鵰鵲鵶鵷鵾鶄鶇鶉鶊鶌鶒"
    "鶓鶖鶗鶘鶚鶠鶡鶥鶦鶩鶪鶬鶭鶯鶰鶱鶲鶴鶹鶺鶻鶼鶿鷀鷁鷂鷄鷅鷈鷉鷊鷐鷓鷔鷖鷗鷙鷚鷟鷣鷤鷥鷦鷨鷩鷫鷭鷯鷰鷲鷳鷴鷷鷸鷹鷺鷽鷿鸂鸇鸊鸋鸌鸎鸏鸑鸕鸗鸘鸚鸛鸝鸞鹵鹹鹺鹻鹼鹽麗麥麨麩麪麫麬麯麲麳麴麵麷麹麺麼麽黃黌點黨"
    "黲黴黶黷黽黿鼂鼃鼇鼈鼉鼕鼴齊齋齎齏齒齔齕齗齘齙齜齟齠齡齣齦齧齩齪齬齭齮齯齰齲齴齶齷齼齾龍龎龐龑龓龔龕龜龭龯鿁鿐鿓"
)


# 常见英文词 → 中文（商品域，覆盖实测泄漏与高频词）
EN_REPLACEMENTS: dict[str, str] = {
    "ball": "球",
    "butterfly": "蝴蝶",
    "heart": "心",
    "star": "星",
    "cup": "杯",
    "box": "盒",
    "card": "卡",
    "gift": "礼物",
    "home": "家",
    "flower": "花",
    "light": "灯",
    "candle": "蜡烛",
    "frame": "相框",
    "bag": "包",
    "ring": "环",
    "tree": "树",
    "bird": "鸟",
    "angel": "天使",
    "rose": "玫瑰",
    "set": "套装",
    "glass": "玻璃",
    "metal": "金属",
    "wood": "木质",
    "paper": "纸",
    "cloth": "布",
    "white": "白色",
    "black": "黑色",
    "red": "红色",
    "pink": "粉色",
    "blue": "蓝色",
    "green": "绿色",
    "gold": "金色",
    "silver": "银色",
    "cream": "奶油色",
    "holder": "支架",
    "hanging": "悬挂",
    "decoration": "装饰",
    "ornament": "装饰品",
    "price": "价格",
    "budget": "预算",
    "quality": "品质",
    "style": "风格",
    "party": "派对",
    "wedding": "婚礼",
    "christmas": "圣诞",
}

_LATIN_RE = re.compile(r"[A-Za-z]{2,}")

# 流畅度门控要求的最少中文字数（防止英文被删空后误判为合规）
MIN_CJK_CHARS = 8


def _load_converter():
    """返回 (convert_fn, engine_name)；无可用转换器时返回 (None, None)。"""
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import zhconv  # type: ignore

        return (lambda text: zhconv.convert(text, "zh-cn")), "zhconv"
    except Exception:
        pass
    try:
        import opencc  # type: ignore

        converter = opencc.OpenCC("t2s")
        return (lambda text: converter.convert(text)), "opencc"
    except Exception:
        return None, None


def find_traditional(text: str) -> list[str]:
    """返回文本中出现的繁体专有字（去重、保序）。"""
    seen: list[str] = []
    for ch in text or "":
        if ch in TRADITIONAL_MARKERS and ch not in seen:
            seen.append(ch)
    return seen


def find_latin(text: str) -> list[str]:
    """返回文本中残留的英文词（去重、保序）。"""
    seen: list[str] = []
    for word in _LATIN_RE.findall(text or ""):
        if word.lower() not in seen:
            seen.append(word.lower())
    return seen


# ------------------------------------------------------------
# 输入侧归一化（与生成侧的 normalize_fluency 严格区分）
# ------------------------------------------------------------
# 只覆盖全角数字与拉丁字母。刻意**不**包含全角中文标点（，。！？：；（））——
# 它们是正常的中文标点，转成 ASCII 会改变文本面貌（实测：逗号被转成 ","）。
_FULLWIDTH_RE = re.compile(r"[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")
# 只匹配「每个数字组都是单个数字」的形态，因此 "300 500" 这类两个独立数字不会被误合并
_SPLIT_DIGITS_RE = re.compile(r"(?<!\d)\d(?:[ \u3000]\d)+(?!\d)")


def _to_halfwidth(text: str) -> str:
    """全角数字与拉丁字母转半角（"３００" → "300"、"ＡＢ" → "AB"）。"""
    return "".join(
        chr(ord(ch) - 0xFEE0)
        if ("\uff10" <= ch <= "\uff19" or "\uff21" <= ch <= "\uff3a" or "\uff41" <= ch <= "\uff5a")
        else ch
        for ch in text
    )


def _collapse_split_digits(text: str) -> str:
    """折叠被空格拆开的数字串："3 0 0 以内" → "300 以内"。"""
    return _SPLIT_DIGITS_RE.sub(lambda m: re.sub(r"[ \u3000]", "", m.group(0)), text)


def normalize_input(text: str) -> tuple[str, list[dict]]:
    """输入侧归一化：繁转简 + 全角转半角 + 定向错别字纠正 + 折叠被空格拆开的数字。

    与 `normalize_fluency` 的关键区别：**绝不删除英文词、绝不截断文本**。
    用户说的话里每个字都可能是信息，这里只做等价改写，并返回处置明细供追溯。
    """
    cfg = settings.INPUT_NORM
    actions: list[dict] = []
    out = text or ""

    if cfg["fullwidth_to_halfwidth"] and _FULLWIDTH_RE.search(out):
        out = _to_halfwidth(out)
        actions.append({"action": "fullwidth_to_halfwidth"})

    if cfg["trad_to_simp"]:
        traditional = find_traditional(out)
        if traditional:
            converter, engine = _load_converter()
            if converter is not None:
                out = converter(out)
                actions.append(
                    {
                        "action": "traditional_to_simplified",
                        "engine": engine,
                        "chars": len(traditional),
                        "sample": traditional[:6],
                    }
                )
            else:
                actions.append(
                    {
                        "action": "traditional_detected_no_converter",
                        "chars": len(traditional),
                        "sample": traditional[:6],
                    }
                )

    for rule in cfg["typo_rules"]:
        pattern = re.compile(rule["pattern"])
        if pattern.search(out):
            out = pattern.sub(rule["replace"], out)
            actions.append({"action": "typo_corrected", "desc": rule["desc"]})

    if cfg["collapse_split_digits"]:
        collapsed = _collapse_split_digits(out)
        if collapsed != out:
            out = collapsed
            actions.append({"action": "collapse_split_digits"})

    return out, actions


def normalize_fluency(text: str) -> tuple[str, list[dict]]:
    """规范化流畅度：繁转简 + 替换常见英文词。返回 (文本, 处置明细)。"""
    actions: list[dict] = []
    out = text or ""

    converter, engine = _load_converter()
    traditional = find_traditional(out)
    if traditional:
        if converter is not None:
            converted = converter(out)
            actions.append(
                {
                    "action": "traditional_to_simplified",
                    "engine": engine,
                    "chars": len(traditional),
                    "sample": traditional[:6],
                }
            )
            out = converted
        else:
            actions.append(
                {"action": "traditional_detected_no_converter", "chars": len(traditional), "sample": traditional[:6]}
            )

    latin = find_latin(out)
    replaced: list[str] = []
    for word in latin:
        target = EN_REPLACEMENTS.get(word)
        if target:
            out = re.sub(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", target, out, flags=re.IGNORECASE)
            replaced.append(word)
    if replaced:
        actions.append({"action": "english_replaced", "words": replaced})

    # 未映射的英文词：**直接删除**而不是保留。
    # 理由：删除不引入任何新内容，是最保守的处理（补词表是打地鼠，永远补不完）。
    # 但若删除后长度骤减（说明原文大量为英文），则判为不合格，交由上层回退模板。
    unmapped = [w for w in find_latin(out) if w.lower() not in EN_REPLACEMENTS]
    if unmapped:
        before_len = len(out)
        for word in unmapped:
            out = re.sub(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", " ", out, flags=re.IGNORECASE)
        out = re.sub(r"\s{2,}", " ", out)
        if before_len and len(out) < before_len * 0.5:
            actions.append(
                {"action": "english_too_much", "words": unmapped, "note": "英文占比过高，删除后信息损失过大"}
            )
        else:
            actions.append({"action": "english_removed", "words": unmapped})

    # 清掉「中文字 + 空格 + 中文字」里的多余空格
    # （英文词被替换成中文后，原词两侧的空格会残留，如「闪粉 蝴蝶 发夹」）
    collapsed = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", out)
    if collapsed != out:
        actions.append({"action": "collapse_cjk_spaces"})
        out = collapsed
    return out, actions


def _cjk_count(text: str) -> int:
    return sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)


def verify_fluency(text: str) -> list[dict]:
    """流畅度门控：繁体未清干净、残留英文词、或中文内容不足，即判不合格。

    最后一条是必要的：若模型输出大量英文，规范化会把英文删掉，
    结果可能只剩标点 —— 这种「删空了」的输出必须被拦下，而不是当成合规。
    """
    violations: list[dict] = []
    if _cjk_count(text) < MIN_CJK_CHARS:
        violations.append(
            {
                "type": "insufficient_chinese",
                "cjk_chars": _cjk_count(text),
                "min": MIN_CJK_CHARS,
                "reason": "规范化后中文内容不足，判定为无效输出",
            }
        )
    traditional = find_traditional(text)
    if traditional:
        violations.append(
            {
                "type": "traditional_chars",
                "chars": traditional[:8],
                "count": len(traditional),
                "reason": "存在繁体字，未通过规范化",
            }
        )
    latin = find_latin(text)
    if latin:
        unmapped = [w for w in latin if w not in EN_REPLACEMENTS]
        if unmapped:
            violations.append({"type": "latin_words", "words": unmapped[:8], "reason": "存在未映射的英文词，中英混排"})
    return violations


def has_too_much_english(actions) -> bool:
    return any(a.get("action") == "english_too_much" for a in (actions or []))


def describe() -> dict:
    _converter, engine = _load_converter()
    return {
        "traditional_markers": len(TRADITIONAL_MARKERS),
        "converter": engine or "none（仅检测，不转换）",
        "english_map_size": len(EN_REPLACEMENTS),
    }

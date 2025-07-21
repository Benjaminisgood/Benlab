# 化学实验室管理系统开发指南
本系统旨在为化学实验室提供一个基于 Flask 的管理平台，涵盖课题组物品管理、课题组成员管理和实验室位置管理三个主要模块。前端采用 HTML/CSS（可借助 Bootstrap 等前端框架统一样式），后端使用 Python Flask 实现业务逻辑。基本数据存储采用轻量级的 SQLite 数据库，并通过 Pandas 库来读取和管理数据实现模块功能 ￼。系统支持多用户登录（无需单独的管理员账户，每位课题组成员凭账号登录），实现物品信息的增删改查、成员个人主页及通知、实验室位置的可视化标注等功能。图片上传和预览也是系统的一项重要功能：用户可直接通过网页表单调用摄像头拍照上传实验室物品或位置图片，并在上传后即时预览效果。 ￼

总体而言，该系统将实验室日常管理的三个方面融合在一个平台上：物品台账、人员信息和实验室空间位置。各模块数据相关联，并在界面上提供便捷的交互。例如，当某物品信息更新时，相关负责人会在其个人主页收到通知提醒；当用户在库存中搜索到某化学品时，可直接查看其存放的具体实验室位置图。接下来将详细介绍系统的数据设计、功能模块和实现细节。

## 技术栈

后端：Flask（Python 框架）
前端：HTML、CSS，推荐使用 Bootstrap 实现响应式 UI
数据库：SQLite（轻量级数据库，适合小型团队）
数据处理：Pandas（用于数据导入/导出或分析）
其他：Flask-SQLAlchemy（ORM 工具）、Flask-Login（用户认证）、Werkzeug（文件上传）

## 数据模型
系统使用 SQLite 作为基础数据库，结合 Pandas 库对数据进行管理和操作 ￼。系统管理三个主要实体：课题组成员、课题组物品和实验室位置，每个实体包含图片路径、备注和上次修改时间。主要包含以下三张数据表：
	•	课题组成员表（Members）：字段包括member_id(主键)、姓名、登录用户名、密码哈希、联系方式等基本信息，以及个人照片路径（存储在服务器指定的图片目录）、备注、最后修改时间等。成员表的记录代表实验室的每个成员账号。

	•	课题组物品表（Items）：字段包括item_id(主键)、物品名称、物品类别或危险级别、当前状态、负责人员（外键关联到成员表）、存放位置（外键关联到位置表）、物品图片路径、备注说明、最后修改时间、购买链接等。物品的“状态”字段可以用来描述库存量以及安全程度等等，例如充足、少量、用完表示库存量状态，或者安全、一般、危险、昂贵等标签描述物品特性。可以对这些特性进行筛选，快速找出对应的物品。

	•	实验室位置表（Locations）：字段包括location_id、位置名称（如“试剂柜A”或“实验室东区”）、负责人员（成员表外键关联）、存有的物品（外键关联到物品表）、位置图片路径、备注、最后修改时间等。位置表的每条记录代表实验室的一个存储区域或房间。
    
    
上述所有表的图片文件统一存放在服务器的一个目录下（如static/images/），数据库中仅保存图片的文件路径或文件名。注意：将图片二进制直接保存到数据库并非最佳实践，因为这会导致数据库体积膨胀并影响查询性能，通常建议将图片保存在文件系统中，数据库中仅存储图片的路径或 URL ￼。本系统遵循这一原则，每次有图片上传时，将文件保存到服务器文件夹，并将路径记录在相应的数据表字段中。

除了修改不频繁的这些基本数据用sqlite之外，其他的都使用pandas来管理，以实现三个系统的主要功能，这样不仅在开发中通过 Pandas 来简化对 SQLite 数据的操作，减少了直接写 SQL 查询的繁琐，而且还实现“所见即所得”的数据处理流程（正如一个开源实验室库存系统所做的：先用 CSV 准备数据，再通过脚本导入 SQLite，无需了解 SQL ￼）。也就是说，除了sqlite里的那些对系统级基本数据的存储外，三个系统的功能实现需要额外的panda制表。制作的多张表格需要可以导出和下载。需要注意，不要直接用 to_sql 全表替换的方式更新数据，在多用户并发时可能覆盖他人改动。下列是具体实现：

由于需要在图片上标记物品位置，还需存储相关的坐标数据：方式是通过网页和pandas表格，获取此位置的所有物品信息，通过拖拽赋予位置坐标，如pos_x和pos_y，用于记录该物品在其所在位置图片中的坐标（相对于图片的百分比或像素值）。这样每件物品都能在对应位置的图片上以图钉形式展示。这就需要一张表格。

再比如所有用户产生的行为记录Logs，也需要额外的表格来记录，这样可以很方便的对这些短时数据进行删除，导出备份等等。并且极其容易查看和供无编程基础的人编辑。这大概需要id (主键), timestamp, user_id (外键), action_type, details等。

再比如用户之间的留言板，也需要有下列的列表：
Messages
id (主键), sender_id (外键), content, timestamp

其他还有一些不太重要的细节，ai自己决策。

## 功能需求

### 物品管理子系统

物品管理是本系统的核心模块之一，支持对课题组物资（试剂、耗材、仪器等）的增删改查和状态跟踪。通过网页界面，用户可以浏览所有物品清单、检索特定物品，并对物品信息进行编辑或新增记录。用户可以通过关键字（如名称的一部分）筛选物品，并按类别（化学品、耗材、设备等）进行过滤。物品列表以表格形式呈现，每行显示物品名称、数量或状态、存放位置以及存放位置的具体区域（如柜子/抽屉）等信息。这样的界面方便用户快速扫描库存详情 ￼。在我们的系统中，也会提供类似的物品列表视图，并根据物品的状态给予不同的视觉标识（例如“用完”状态的物品用红色高亮）。

每个物品记录包含如下主要信息：名称、状态、负责人、存放位置、上次更新时间、备注，以及关联的图片（如化学品瓶子的照片或标签）。用户点击某一物品名称，可进入该物品的详情页面或编辑页面，在那里可以查看完整信息并执行修改。编辑表单允许更新上述各字段——其中状态字段可从预定义选项选择，例如库存量状态（充足/少量/用完）或危险级别（安全/一般/危险/昂贵等）。负责人字段通常是从成员列表中选择，将物品责任归属到具体的人。

检索与筛选功能： 用户可以通过搜索框按名称、类别或负责人来检索物品。实现上，可以在服务端利用 Pandas 对物品 DataFrame 进行过滤，根据查询字符串匹配名称或其他字段，然后将结果传递给模板渲染。如果数据集较大，也可以考虑使用 AJAX 结合前端脚本实现动态搜索提示。简单情况下，在 Flask 路由中获取搜索参数后，用 DataFrame 的布尔索引筛选，再将筛选结果转为列表供模板显示即可。

增删改操作： 系统提供“新增物品”入口，点击后跳转到物品信息录入表单页面。输入包括物品名称、选择负责人、选择存放位置（可从已有位置列表中选择）、状态、备注，以及上传物品图片（可选）。提交后，由 Flask 后端接收表单数据，通过 SQL INSERT 或 Pandas 将新记录加入数据库，并记录当前时间为最后更新时间。编辑物品时流程类似，只是先读出旧数据填充表单，用户修改后提交，后端执行 UPDATE。删除物品操作可根据需要提供，一般在列表页面为每条记录提供“删除”按钮，点选后弹出确认，再由后端执行 DELETE 语句移除记录。

协作编辑与通知： 由于没有专门管理员账户，所有课题组成员都可以参与物品信息的维护。这要求一定的操作记录和提醒机制。例如，当某用户修改了一件他人负责的物品时，系统应记录这一操作，并在物品详情或负责人的主页上显示该修改记录。实现方式是在每次修改物品时，在“最近更新时间”和“最近更新人”等字段更新之外，向日志表插入一条记录，包括物品ID、操作人、时间和动作类型（修改/新增/删除）。日志数据可用于稍后成员主页的通知展示。负责人看到有人更新了自己负责的物品时，可以及时知晓变更。


### 组员管理子系统

组员管理模块主要面向每个课题组成员的个人信息和动态展示。每位成员都有自己的个人主页，在登录后可以查看。个人主页包含以下内容：
	•	基本信息：成员的姓名、照片头像、联系方式等基本资料，来自成员表。成员可以更新自己的个人信息（如上传新头像或修改备注）。
	•	负责物品和位置列表：展示该成员目前负责的所有物品清单和实验室位置清单。例如，一个成员负责某些试剂和设备，则在其主页上列出这些物品名称（可链接到物品详情），以及他作为负责人所管理的实验室位置（例如某个试剂柜由他负责）。这样成员可以方便地查看与自己有关的所有资产。
	•	通知提醒：当与成员相关的物品或位置信息发生改动时（例如他负责的物品被别人编辑了，或他负责的实验室区域增加了新物品），系统会生成一条通知。在个人主页上，这些通知会列出最近的动态，例如：“你的负责物品 X试剂 信息于昨日被更新”或“你管理的 试剂柜A 新增了物品 Y”。这部分功能可通过维护一张活动日志表或通知表实现。在物品或位置更新的代码中检查其负责人，如果不是当前操作人，则为负责人创建通知记录。页面加载时，再过滤出当前用户相关的未读通知列表进行显示 ￼ ￼。用户查看后可将通知标记为已读（或系统简单地在页面显示最近若干条即可）。
	•	个人展示板和留言板：个人展示板可以让成员发布一些状态或介绍（可选功能），但题目中特别提到了“留言板”，意味着其它成员可以在某人主页上留下留言。这类似于简单的讨论区或评论功能。实现时可为每个成员设一个留言列表存储在数据库中（例如 messages 表，字段包括留言ID、发送者、接收者、内容、时间）。在成员主页下方展示所有给该成员的留言，并提供一个留言表单，允许其他登录用户输入内容提交。后端收到留言后插入 messages 表，并刷新该成员主页显示。这样组内成员可以彼此交流，例如提醒补充某物品库存等。
	•	个人操作记录：成员主页还可以列出该用户自身的操作日志，比如“你于2025-07-20添加了物品 X”“你于2025-07-18编辑了实验室位置 Y”等。这通过查询日志表中过滤出user_id等于当前用户的记录实现，类似地可以用 Pandas 读取日志 DataFrame 后按用户筛选。此功能让用户回顾自己在系统中的活动历史，提高责任追踪性。

从实现角度来看，成员管理需要建立用户认证机制（下一节详述），确保每个用户只能修改自己的信息或进行登录后操作。成员主页通常对应 URL 路径如/member/<username>或/profile（后者通过session识别用户）。视图函数需要从数据库提取该成员的相关信息：Members表基本信息，Items表中负责人是该成员的条目列表，Locations表中负责人是该成员的条目列表，Logs/Notifications表中过去的相关活动，Messages表中收件人为该成员的留言。可以使用多次 SQL 查询或一次联合查询获取，也可以使用 Pandas 分别读取表再进行合并。取出结果后，将其传给模板，在页面上做循环显示列表。同时，提供必要的链接以跳转到具体物品或位置详情，方便进一步操作。

每个人可以查看别人和自己的主页，可以在留言板留言（pandas建表记录），可以编辑自己的展示板（可以用数据表单里的备注）


### 实验室位置管理子系统

实验室位置管理模块以空间可视化的方式帮助用户了解物品分布。现在每个实验室位置（如房间、柜子、冰箱等）都有一张关联的图片（比如房间布局图或柜内分隔示意图，也就是位置的一个表属性，sqlite里记录了图片路径的），系统允许将该位置所含物品用**大头针（图钉）**的方式标记在图片上。所有成员都可以参与定位：即大家可以将某物品的图钉放置在实验室位置图片的正确位置上，以便组员点击图钉即可查看对应物品信息和负责人。

比如我们在“实验室布局”图像上放置了一些带名称标签的定位针（蓝色图标），标记出不同存储区的位置示意。右侧的列表列出了这些位置的名称。点击图像中的图钉，可以弹出显示该位置或物品的详细信息。这种映射功能的实现，将抽象的存储位置和实体空间对应起来，使实验室成员在搜索到某物品后，可以直观地找到它在房间中的位置 ￼。

在本系统中，每当用户打开一个具体的实验室位置页面（例如“试剂柜A”页面），后台会加载该位置的图片以及属于此位置的所有物品坐标。页面前端通过 HTML 和 CSS/JavaScript 将图钉渲染叠加在图片上。具体实现步骤如下：

1.	坐标数据准备：物品表中需要有记录每件物品在图片上的相对坐标（如pos_x, pos_y百分比）。这些坐标可以在物品添加/编辑时由用户指定，或者在位置页面进入“编辑模式”时通过点击选取。初始可能很多物品未设置坐标，页面可提供“添加标记”按钮。

2.	显示图片与图钉：在模板中，使用一个容器 <div> 来包裹位置图片，并设置 position: relative;。然后对于每个有坐标的物品，用一个绝对定位的元素（如<img src="pin_icon.png">或一个带定位图标的 <span>）放入该容器。通过CSS将其left设为pos_x%，top设为pos_y%，从而使图钉出现在相对位置上。每个图钉元素带有比如data-item-id属性，或者做成一个链接，当用户点击时，可以触发显示该物品详情的小弹窗（可用Bootstrap的Tooltip/Popover，或简单实现一个隐藏的显示信息）。

3.	添加/编辑标记：当用户点击“添加标记”后，页面可以启用一个JS事件监听图片的点击位置。用户在图像上某处单击，捕获鼠标相对于图片左上角的坐标，通过图片宽高换算得到百分比位置。随后可以弹出一个对话框，列出当前地点尚未定位的物品让用户选择，或直接输入物品信息以新建。提交后，后台pandas将在表格中保存该物品的坐标（如果新建则也插入sqlite的物品表）。下次加载时，该物品图钉就会出现在相应位置。
 ￼ ￼
4.	移动或删除标记：对于已有的图钉，也应提供调整功能。例如拖拽图钉到新位置，或删除图钉。当拖拽完成时，使用JS获取新坐标并通过 AJAX 提交更新物品记录中的坐标字段。删除图钉可以理解为物品不再存放于该位置，可能需要更新物品的 location_id 或标记其坐标无效。

5.	位置详情：实验室位置页面除了图片和图钉，还应显示该位置的基本信息（例如备注、负责人等）以及物品列表（所有 location_id 指向此位置的物品清单）。这样用户可以在位置页面同时看到图片标记和文字列表，两者同步。列表中的物品若缺少坐标标记，可以用特殊样式标注提醒用户去定位它。

通过上述功能，实验室成员能够方便地将库房、柜子的实际布局和系统数据相关联。例如，新进了一瓶化学试剂，用户在物品管理中登记后，可以立刻在相应柜子的图片上标记它的位置；其他成员日后查看时，只需点击柜子图上的图钉就能知道该试剂放在哪一层哪一格 ￼。这种直观的管理方式提高了实验室管理效率和安全性。


### 用户登录与权限

由于系统不设管理员账户，但需要区分不同成员的身份，因此用户登录系统是必要的。登录系统保证只有课题组内部成员（已在成员表中注册的用户）才能访问受保护的功能页面，并根据登录身份提供个性化内容（如个人主页、编辑权限等）。

实现思路：
	•	用户注册：如果预先在数据库中建立了所有成员账号，也可以省略自助注册功能。如果需要注册，则提供注册表单，收集用户名、密码、邮箱等信息，创建新成员记录。密码应采用安全散列算法保存（如Werkzeug提供的generate_password_hash），确保不会明文存储密码。
	•	登录：提供登录页面（/login），让用户输入用户名和密码。Flask 后端验证密码正确后，使用 Flask 的会话机制或 Flask-Login 扩展将用户标记为已登录状态 ￼。例如，Flask-Login 可以很方便地管理用户 session，实现登录、登出等 ￼。登录成功后跳转到主界面或个人主页，登录失败则给出错误提示并可重试。
	•	访问控制：使用登录装饰器或判断会话的方法，保护需要登录才能访问的路由。例如物品列表、成员主页、位置管理等路由在未登录时重定向到登录页。Flask-Login 提供了@login_required装饰器简化这个过程。由于系统中所有登录用户权限基本相同（都可以增改物品等），因此主要的权限控制是需登录，而不区分更细的角色。某些敏感操作可以增加额外检查，例如只有负责人才收到通知或可以修改负责字段等，这在视图逻辑中通过对比当前用户与记录负责人来决定。
	•	登出：提供登出按钮，调用 Flask-Login 的 logout_user 或者手动pop会话，以销毁登录状态。
	•	密码安全：除散列存储密码外，可以考虑防止暴力破解（例如登录失败次数过多锁定一段时间），或使用验证码 ￼等增强安全性。不过在小型实验室内部应用中，此要求可以酌情降低。这里我们是小型实验室，所以代码从简。


### 文件上传与图片预览

文件（图片）上传是本系统的关键辅助功能，涉及成员头像、物品照片以及实验室位置平面图等上传。为了提升用户体验，系统需要实现直接拍照上传和即时预览。具体而言，在相关的表单页面（如新增/编辑物品，编辑成员信息，新增/编辑位置）中，图片字段应允许用户调取摄像头拍照或从文件系统选择图片，并在提交前于页面上预览缩略图效果。

前端实现： 利用 HTML5 文件输入元素实现。
其中accept="image/*"限定只能选择图像文件，capture="environment"提示在移动设备上调用后摄像头直接拍照 ￼（如在手机浏览器中会直接打开相机）。然后，通过少量 JavaScript 实现选择文件后的预览：监听 <input type="file"> 的变化事件，读取所选文件生成本地 URL 或 DataURL，在标签上显示出来，并取消隐藏它。这使得用户在提交前就能看到拍摄或选取的照片缩略图，确认无误。

后端实现： Flask 处理文件上传需要在 <form> 标签上指定 enctype="multipart/form-data"，并使用 request.files 来获取文件对象 ￼。
代码中，我们检查了文件存在且文件名非空，然后用 secure_filename 清理文件名并保存到指定UPLOAD_FOLDER目录 ￼ ￼。保存成功后，把文件名（或相对路径）存入数据库中物品表的图片字段。这样，在日后显示时，可以通过例如<img src="{{ url_for('static', filename='images/'+item.image_path) }}">来加载该图片。

为了防止服务器存储被不必要地占用，可以对上传图片作一些处理，例如使用 Pillow 库调整图像大小或压缩质量，或者定期清理长时间未使用的图片。但这些属于优化范围。基本实现上，如Flask官方文档所示，只需几行代码即可处理文件上传和保存 ￼ ￼。

注意事项：
	•	预览安全：由于直接在浏览器显示本地选择的图片不涉及服务器交互，安全风险较低。但上传后应当在服务器端验证文件类型是否真的是图像（例如通过读取文件头或使用Imaging库），并限制文件大小，防止恶意用户上传特洛伊木马或超大文件 ￼。可在 Flask 配置MAX_CONTENT_LENGTH限制上传大小，如16MB等 ￼。
	•	存储路径：如前所述，所有上传的图片都保存在 Flask 的 static 目录下的某统一子目录中，例如static/images/。这确保 Flask 可以直接服务器端提供这些文件 ￼ ￼。数据库仅存相对路径或文件名，避免存储绝对路径带来的迁移问题。
	•	多图管理：对某些实体，如果需要上传多张相关图片，也可以扩展为单独的图片表或在文件名中加入记录ID区分，但本系统需求中每个物品/位置/成员只有一张主要图片，故不需复杂设计。

### 前端界面与部署

为了让三个子系统有一致、友好的用户界面，我们可以采用现有的前端 UI 框架例如 Bootstrap、Semantic UI 或 Material Design。这些框架提供了统一风格的导航栏、表单控件、按钮和布局栅格系统，可以大大提升界面美观度和开发效率。具体设计上：
	•	导航布局：应用有全局导航栏，包含页面跳转链接，例如“物品管理”、“实验室地图”、“成员列表/个人主页”等。登录后的用户导航栏还可显示其用户名及登出按钮。可以利用 Flask 模板继承机制创建一个base.html，定义导航和公共样式，各页面模板再继承 base。
	•	列表与表单：物品列表页、成员列表页采用表格或者卡片列表显示记录。Bootstrap 的表格样式可以提供基本的条纹行、高亮悬停等效果。表单页面则使用表单组(<div class="form-group">等)来排列输入字段和标签，配合简洁的 CSS 提示。使用这些现成组件有助于保持各模块界面的一致性。
	•	响应式：由于可能成员会用手机直接拍照上传，界面需要在移动端有效运作。Bootstrap 等框架天生具有响应式支持，使布局能适应不同屏幕。尤其是实验室位置的图片标记页面，我们可以设定图片容器宽度为100%父元素，这样在小屏幕上图片会自动缩放，图钉的位置我们用百分比也能随之缩放正确 ￼。
	•	交互增强：可以使用少量 JavaScript/jQuery来增强用户体验。例如，物品列表页可以引入 DataTables 插件，实现客户端分页、即时搜索和排序；物品详情或位置图钉点击可以用模态框弹出详细信息；留言板的提交可以用 AJAX 刷新局部区域等等。这些不是必须功能，但可以作为改进方向。

部署运行： 开发完成后，该 Flask 应用可以作为一个可独立运行的服务部署。在开发环境下，直接运行 app.py (其中包含app.run())即可在本地启动测试服务 ￼。部署到生产环境时，可以选择将 Flask 应用通过 WSGI 部署，例如使用 Gunicorn 或 uWSGI 配合 Nginx 等。由于本项目使用 SQLite，本地文件即可持久化数据，无需额外的数据库服务，非常适合小型团队内网部署。只需确保静态文件夹有读权限、上传文件夹有写权限，以及设置一个安全的 SECRET_KEY 用于会话管理。

在启动服务后，用户可以通过浏览器访问相应 IP 和端口（默认127.0.0.1:5000）看到登录界面并使用系统 ￼。若将其部署在实验室局域网上的服务器，所有组内成员便可共同访问这套管理系统，实现协同管理实验室的目的。

总结： 这套 Flask 化学实验室管理系统以 SQLite+Pandas 为数据支撑，提供了物品、人员、位置三个维度的管理功能，并通过图片和图钉将物品与实际位置关联 ￼。系统实现过程中，注重了用户体验（如摄像头上传、图片预览、通知提醒）以及代码的清晰组织（使用蓝图划分模块，模板继承重用布局等）。按照上述设计完成开发和调试后，即可部署为一个完整可运行的应用，帮助课题组有效地管理实验室资源和信息。通过这个平台，库存清单、人员分工、物品位置都一目了然，大大提高了实验室管理的效率和安全性。每个用户也能通过系统方便地获取与自己相关的最新动态，实现信息的及时沟通与共享。 ￼ ￼


所有的内容完整包括，输出方式按照最佳方式来，保证我每次都可以顺利运行！运行环境是macmini m4，版本都用最新的！














# ai代码
化学实验室管理系统 – 完整代码

运行环境依赖:
	•	Python 3.x (建议使用最新版本，如 3.10+)
	•	Flask (最新版本)
	•	Flask-Login (最新版本)
	•	Flask-SQLAlchemy (最新版本)
	•	Pandas (用于数据导出)

注: 请确保安装上述依赖库 (pip install flask flask-login flask-sqlalchemy pandas) 并在项目根目录下创建 static/images 文件夹用于保存上传的图片。以下是完整的代码文件列表，请按照对应文件名和路径保存。在终端进入项目目录后，运行 python app.py 启动 Flask 应用。

app.py (Flask 后端代码)

import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkey'  # 为安全起见，部署时请使用更复杂的随机值
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///lab.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'images')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 限制上传16MB以内的文件

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# 允许上传的图片扩展名
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 数据模型定义
class Member(UserMixin, db.Model):
    __tablename__ = 'members'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)            # 姓名
    username = db.Column(db.String(100), unique=True, nullable=False)  # 登录用户名
    password_hash = db.Column(db.String(200), nullable=False)    # 密码哈希
    contact = db.Column(db.String(100))                          # 联系方式（邮箱/电话）
    photo = db.Column(db.String(200))                            # 头像图片路径
    notes = db.Column(db.Text)                                   # 备注/个人展示板
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    # 关系：成员负责的物品和位置，以及发送/收到的消息和日志
    items = db.relationship('Item', backref='responsible_member', lazy=True, foreign_keys='Item.responsible_id')
    locations = db.relationship('Location', backref='responsible_member', lazy=True, foreign_keys='Location.responsible_id')
    sent_messages = db.relationship('Message', backref='sender', lazy=True, foreign_keys='Message.sender_id')
    received_messages = db.relationship('Message', backref='receiver', lazy=True, foreign_keys='Message.receiver_id')
    logs = db.relationship('Log', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    def __repr__(self):
        return f'<Member {self.username}>'

class Item(db.Model):
    __tablename__ = 'items'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)    # 物品名称
    category = db.Column(db.String(50))                 # 类别/危险级别
    status = db.Column(db.String(50))                   # 当前状态（库存/危险等）
    responsible_id = db.Column(db.Integer, db.ForeignKey('members.id'))  # 负责人（成员ID）
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))   # 存放位置（位置ID）
    image = db.Column(db.String(200))                   # 图片文件名
    notes = db.Column(db.Text)                          # 备注说明
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    purchase_link = db.Column(db.String(200))           # 购买链接
    pos_x = db.Column(db.Float)                         # 在位置图片上的标记X坐标（百分比）
    pos_y = db.Column(db.Float)                         # 在位置图片上的标记Y坐标（百分比）
    logs = db.relationship('Log', backref='item', lazy=True)  # 操作日志

    def __repr__(self):
        return f'<Item {self.name}>'

class Location(db.Model):
    __tablename__ = 'locations'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)    # 位置名称
    responsible_id = db.Column(db.Integer, db.ForeignKey('members.id'))  # 负责人（成员ID）
    image = db.Column(db.String(200))                   # 位置图片文件名
    notes = db.Column(db.Text)                          # 备注
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)  # 最后修改时间
    items = db.relationship('Item', backref='location', lazy=True, foreign_keys='Item.location_id')
    logs = db.relationship('Log', backref='location', lazy=True)     # 操作日志

    def __repr__(self):
        return f'<Location {self.name}>'

class Log(db.Model):
    __tablename__ = 'logs'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('members.id'))     # 执行操作的用户ID
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=True)         # 涉及的物品ID（如果有）
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=True) # 涉及的位置ID（如果有）
    action_type = db.Column(db.String(50))       # 操作类型描述（如 新增物品/修改位置 等）
    details = db.Column(db.Text)                 # 详情备注
    def __repr__(self):
        return f'<Log {self.action_type} by {self.user_id}>'

class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('members.id'))    # 发送者用户ID
    receiver_id = db.Column(db.Integer, db.ForeignKey('members.id'))  # 接收者用户ID
    content = db.Column(db.Text, nullable=False)     # 留言内容
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)  # 留言时间
    def __repr__(self):
        return f'<Message from {self.sender_id} to {self.receiver_id}>'

# 初始化数据库并创建默认用户
with app.app_context():
    db.create_all()
    if Member.query.count() == 0:
        default_user = Member(name="Admin User", username="admin", contact="admin@example.com", notes="Default admin user")
        default_user.set_password("admin")
        db.session.add(default_user)
        db.session.commit()

@login_manager.user_loader
def load_user(user_id):
    return Member.query.get(int(user_id))

# 路由定义
@app.route('/')
def index():
    # 未登录则跳转到登录页，已登录则进入个人主页
    if current_user.is_authenticated:
        return redirect(url_for('profile', member_id=current_user.id))
    else:
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = Member.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('用户名或密码不正确', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form.get('name')
        username = request.form.get('username')
        password = request.form.get('password')
        contact = request.form.get('contact')
        # 检查用户名是否已存在
        if Member.query.filter_by(username=username).first():
            flash('用户名已存在', 'warning')
        else:
            new_user = Member(name=name, username=username, contact=contact)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            flash('注册成功，请登录', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/items')
@login_required
def items():
    # 物品列表，支持按名称/备注搜索，按类别筛选
    search = request.args.get('search', '')
    category_filter = request.args.get('category', '')
    items_query = Item.query
    if search:
        items_query = items_query.filter(Item.name.contains(search) | Item.notes.contains(search))
    if category_filter:
        items_query = items_query.filter_by(category=category_filter)
    items_list = items_query.order_by(Item.name).all()
    # 获取现有类别列表供筛选选项
    categories = [c for (c,) in db.session.query(Item.category).distinct() if c]
    return render_template('items.html', items=items_list, search=search, category=category_filter, categories=categories)

@app.route('/items/<int:item_id>')
@login_required
def item_detail(item_id):
    # 查看物品详情
    item = Item.query.get_or_404(item_id)
    return render_template('item_detail.html', item=item)

@app.route('/items/add', methods=['GET', 'POST'])
@login_required
def add_item():
    if request.method == 'POST':
        # 获取表单数据并创建新物品
        name = request.form.get('name')
        category = request.form.get('category')
        status = request.form.get('status')
        responsible_id = request.form.get('responsible_id')
        location_id = request.form.get('location_id')
        notes = request.form.get('notes')
        purchase_link = request.form.get('purchase_link')
        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            # 保存上传的物品图片文件
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            image_filename = filename
        new_item = Item(
            name=name, category=category, status=status,
            responsible_id=responsible_id if responsible_id else None,
            location_id=location_id if location_id else None,
            notes=notes, purchase_link=purchase_link, image=image_filename
        )
        db.session.add(new_item)
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, item_id=new_item.id, action_type="新增物品", details=f"Added item {new_item.name}")
        db.session.add(log)
        db.session.commit()
        flash('物品已添加', 'success')
        return redirect(url_for('items'))
    # GET 请求时，返回物品添加表单
    members = Member.query.all()
    locations = Location.query.all()
    return render_template('item_form.html', members=members, locations=locations, item=None)

@app.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_item(item_id):
    item = Item.query.get_or_404(item_id)
    if request.method == 'POST':
        # 更新物品信息
        item.name = request.form.get('name')
        item.category = request.form.get('category')
        item.status = request.form.get('status')
        item.responsible_id = request.form.get('responsible_id')
        item.location_id = request.form.get('location_id')
        item.notes = request.form.get('notes')
        item.purchase_link = request.form.get('purchase_link')
        # 处理图片更新（如果有新上传）
        image_file = request.files.get('image')
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            # 可选：删除旧图片文件以节省空间（此处暂不删除）
            item.image = filename
        item.last_modified = datetime.utcnow()
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, item_id=item.id, action_type="修改物品", details=f"Edited item {item.name}")
        db.session.add(log)
        db.session.commit()
        flash('物品信息已更新', 'success')
        return redirect(url_for('items'))
    members = Member.query.all()
    locations = Location.query.all()
    return render_template('item_form.html', members=members, locations=locations, item=item)

@app.route('/items/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    # 记录日志
    log = Log(user_id=current_user.id, item_id=item_id, action_type="删除物品", details=f"Deleted item {item.name}")
    db.session.add(log)
    db.session.commit()
    flash('物品已删除', 'info')
    return redirect(url_for('items'))

@app.route('/locations')
@login_required
def locations_list():
    locations = Location.query.order_by(Location.name).all()
    return render_template('locations.html', locations=locations)

@app.route('/locations/add', methods=['GET', 'POST'])
@login_required
def add_location():
    if request.method == 'POST':
        # 获取并保存新的位置记录
        name = request.form.get('name')
        responsible_id = request.form.get('responsible_id')
        notes = request.form.get('notes')
        image_file = request.files.get('image')
        image_filename = None
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            image_filename = filename
        new_loc = Location(name=name, responsible_id=responsible_id if responsible_id else None, notes=notes, image=image_filename)
        db.session.add(new_loc)
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, location_id=new_loc.id, action_type="新增位置", details=f"Added location {new_loc.name}")
        db.session.add(log)
        db.session.commit()
        flash('实验室位置已添加', 'success')
        return redirect(url_for('locations_list'))
    members = Member.query.all()
    return render_template('location_form.html', members=members, location=None)

@app.route('/locations/<int:loc_id>/delete', methods=['POST'])
@login_required
def delete_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    db.session.delete(location)
    db.session.commit()
    # 记录日志
    log = Log(user_id=current_user.id, location_id=loc_id, action_type="删除位置", details=f"Deleted location {location.name}")
    db.session.add(log)
    db.session.commit()
    flash('实验室位置已删除', 'info')
    return redirect(url_for('locations_list'))

@app.route('/locations/<int:loc_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    if request.method == 'POST':
        # 更新位置信息
        location.name = request.form.get('name')
        location.responsible_id = request.form.get('responsible_id')
        location.notes = request.form.get('notes')
        image_file = request.files.get('image')
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            location.image = filename
        location.last_modified = datetime.utcnow()
        db.session.commit()
        # 记录日志
        log = Log(user_id=current_user.id, location_id=location.id, action_type="修改位置", details=f"Edited location {location.name}")
        db.session.add(log)
        db.session.commit()
        flash('位置信息已更新', 'success')
        return redirect(url_for('locations_list'))
    members = Member.query.all()
    return render_template('location_form.html', members=members, location=location)

@app.route('/locations/<int:loc_id>')
@login_required
def view_location(loc_id):
    location = Location.query.get_or_404(loc_id)
    # 获取该位置包含的所有物品及其坐标
    items_at_location = Item.query.filter_by(location_id=loc_id).all()
    return render_template('location.html', location=location, items=items_at_location)

@app.route('/locations/<int:loc_id>/set_item_position', methods=['POST'])
@login_required
def set_item_position(loc_id):
    # AJAX/表单提交物品坐标标记
    item_id = request.form.get('item_id')
    pos_x = request.form.get('pos_x')
    pos_y = request.form.get('pos_y')
    item = Item.query.get_or_404(item_id)
    if item.location_id == loc_id:
        try:
            item.pos_x = float(pos_x)
            item.pos_y = float(pos_y)
        except:
            item.pos_x = None
            item.pos_y = None
        db.session.commit()
        log = Log(user_id=current_user.id, item_id=item.id, action_type="标记位置", details=f"Set position for item {item.name} in location {loc_id}")
        db.session.add(log)
        db.session.commit()
        flash(f"物品 {item.name} 的位置已更新", "success")
    else:
        flash("无效的物品或位置", "danger")
    return redirect(url_for('view_location', loc_id=loc_id))

@app.route('/members')
@login_required
def members_list():
    members = Member.query.order_by(Member.name).all()
    return render_template('members.html', members=members)

@app.route('/member/<int:member_id>')
@login_required
def profile(member_id):
    member = Member.query.get_or_404(member_id)
    # 该成员负责的物品和位置列表
    items_resp = Item.query.filter_by(responsible_id=member.id).all()
    locations_resp = Location.query.filter_by(responsible_id=member.id).all()
    # 通知列表：他人对该成员负责的物品/位置的最近更新
    notifications = []
    if member.id == current_user.id:
        from sqlalchemy import or_
        notifications = Log.query.join(Item, Log.item_id == Item.id, isouter=True) \
                        .join(Location, Log.location_id == Location.id, isouter=True) \
                        .filter(or_(Item.responsible_id == member.id, Location.responsible_id == member.id), Log.user_id != member.id) \
                        .order_by(Log.timestamp.desc()).limit(5).all()
    # 留言板消息列表（发送给该成员的留言）
    messages = Message.query.filter_by(receiver_id=member.id).order_by(Message.timestamp.desc()).all()
    # 当前用户自己的操作记录（仅查看自己的主页时显示）
    user_logs = []
    if member.id == current_user.id:
        user_logs = Log.query.filter_by(user_id=member.id).order_by(Log.timestamp.desc()).limit(5).all()
    return render_template('profile.html', profile_user=member, items_resp=items_resp, locations_resp=locations_resp, notifications=notifications, messages=messages, user_logs=user_logs)

@app.route('/member/<int:member_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_profile(member_id):
    if current_user.id != member_id:
        flash('无权编辑他人信息', 'danger')
        return redirect(url_for('profile', member_id=member_id))
    member = Member.query.get_or_404(member_id)
    if request.method == 'POST':
        # 更新个人信息
        member.name = request.form.get('name')
        member.contact = request.form.get('contact')
        member.notes = request.form.get('notes')
        # 如填写了新密码则更新密码
        new_password = request.form.get('password')
        if new_password and new_password.strip() != '':
            member.set_password(new_password)
        # 更新头像
        image_file = request.files.get('photo')
        if image_file and image_file.filename != '' and allowed_file(image_file.filename):
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            filename = secure_filename(image_file.filename)
            filename = datetime.now().strftime("%Y%m%d%H%M%S_") + filename
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            member.photo = filename
        member.last_modified = datetime.utcnow()
        db.session.commit()
        flash('个人信息已更新', 'success')
        return redirect(url_for('profile', member_id=member_id))
    return render_template('edit_profile.html', member=member)

@app.route('/message/<int:member_id>', methods=['POST'])
@login_required
def post_message(member_id):
    # 提交留言
    receiver = Member.query.get_or_404(member_id)
    content = request.form.get('content')
    if content and content.strip() != '':
        msg = Message(sender_id=current_user.id, receiver_id=receiver.id, content=content.strip())
        db.session.add(msg)
        db.session.commit()
        flash('留言已发布', 'success')
    return redirect(url_for('profile', member_id=member_id))

@app.route('/export/<string:datatype>')
@login_required
def export_data(datatype):
    # 导出数据为 CSV 文件
    import pandas as pd
    filename = f"export_{datatype}.csv"
    if datatype == 'items':
        df = pd.read_sql(Item.query.statement, db.session.bind)
    elif datatype == 'members':
        df = pd.read_sql(Member.query.statement, db.session.bind)
    elif datatype == 'locations':
        df = pd.read_sql(Location.query.statement, db.session.bind)
    elif datatype == 'logs':
        df = pd.read_sql(Log.query.statement, db.session.bind)
    elif datatype == 'messages':
        df = pd.read_sql(Message.query.statement, db.session.bind)
    else:
        flash('未知数据类型', 'warning')
        return redirect(url_for('index'))
    df.to_csv(filename, index=False)
    return send_file(filename, as_attachment=True, mimetype='text/csv', download_name=filename)

if __name__ == '__main__':
    app.run(debug=False)

templates/base.html

<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>化学实验室管理系统</title>
  <!-- 引入 Bootstrap CSS -->
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    /* 图钉图标的样式 */
    .pin-icon {
      position: absolute;
      transform: translate(-50%, -100%);
      font-size: 24px;
      cursor: pointer;
    }
    .pin-icon:hover {
      opacity: 0.7;
    }
  </style>
</head>
<body>
  <!-- 导航栏 -->
  <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
    <div class="container-fluid">
      <a class="navbar-brand" href="{{ url_for('index') }}">实验室管理系统</a>
      <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
        <span class="navbar-toggler-icon"></span>
      </button>
      <div class="collapse navbar-collapse" id="navbarNav">
        <ul class="navbar-nav me-auto">
          {% if current_user.is_authenticated %}
          <li class="nav-item">
            <a class="nav-link" href="{{ url_for('items') }}">物品管理</a>
          </li>
          <li class="nav-item">
            <a class="nav-link" href="{{ url_for('locations_list') }}">实验室位置</a>
          </li>
          <li class="nav-item">
            <a class="nav-link" href="{{ url_for('members_list') }}">课题组成员</a>
          </li>
          {% endif %}
        </ul>
        <ul class="navbar-nav ms-auto">
          {% if current_user.is_authenticated %}
          <li class="nav-item dropdown">
            <a class="nav-link dropdown-toggle" href="#" id="userMenu" role="button" data-bs-toggle="dropdown">
              {{ current_user.name or current_user.username }}
            </a>
            <ul class="dropdown-menu dropdown-menu-end" aria-labelledby="userMenu">
              <li><a class="dropdown-item" href="{{ url_for('profile', member_id=current_user.id) }}">个人主页</a></li>
              <li><a class="dropdown-item" href="{{ url_for('edit_profile', member_id=current_user.id) }}">编辑个人信息</a></li>
              <li><hr class="dropdown-divider"></li>
              <li><a class="dropdown-item" href="{{ url_for('logout') }}">退出</a></li>
            </ul>
          </li>
          {% else %}
          <li class="nav-item"><a class="nav-link" href="{{ url_for('login') }}">登录</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('register') }}">注册</a></li>
          {% endif %}
        </ul>
      </div>
    </div>
  </nav>
  <!-- 提示消息 -->
  <div class="container mt-3">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, msg in messages %}
        <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
          {{ msg }}
          <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
        {% endfor %}
      {% endif %}
    {% endwith %}
  </div>
  <!-- 主内容 -->
  <div class="container mt-4">
    {% block content %}{% endblock %}
  </div>

  <!-- 引入 Bootstrap JS (含 Popper) -->
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
  <!-- 启用 Bootstrap 工具提示（如需） -->
  <script>
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'))
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
      return new bootstrap.Tooltip(tooltipTriggerEl)
    })
  </script>
</body>
</html>

templates/login.html

{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center">
  <div class="col-md-4">
    <h3 class="mb-4">用户登录</h3>
    <form method="post" action="{{ url_for('login') }}">
      <div class="mb-3">
        <label for="username" class="form-label">用户名</label>
        <input type="text" class="form-control" id="username" name="username" required>
      </div>
      <div class="mb-3">
        <label for="password" class="form-label">密码</label>
        <input type="password" class="form-control" id="password" name="password" required>
      </div>
      <button type="submit" class="btn btn-primary">登录</button>
      <a href="{{ url_for('register') }}" class="btn btn-link">没有账号？注册</a>
    </form>
  </div>
</div>
{% endblock %}

templates/register.html

{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center">
  <div class="col-md-4">
    <h3 class="mb-4">用户注册</h3>
    <form method="post" action="{{ url_for('register') }}">
      <div class="mb-3">
        <label for="name" class="form-label">姓名</label>
        <input type="text" class="form-control" id="name" name="name" required>
      </div>
      <div class="mb-3">
        <label for="username" class="form-label">用户名</label>
        <input type="text" class="form-control" id="username" name="username" required>
      </div>
      <div class="mb-3">
        <label for="password" class="form-label">密码</label>
        <input type="password" class="form-control" id="password" name="password" required>
      </div>
      <div class="mb-3">
        <label for="contact" class="form-label">联系方式 (邮箱或电话)</label>
        <input type="text" class="form-control" id="contact" name="contact">
      </div>
      <button type="submit" class="btn btn-primary">注册</button>
      <a href="{{ url_for('login') }}" class="btn btn-link">已有账号？登录</a>
    </form>
  </div>
</div>
{% endblock %}

templates/items.html

{% extends "base.html" %}
{% block content %}
<h3 class="mb-3">物品列表</h3>
<form class="row g-3 mb-3" method="get" action="{{ url_for('items') }}">
  <div class="col-auto">
    <input type="text" name="search" value="{{ search }}" class="form-control" placeholder="搜索物品...">
  </div>
  <div class="col-auto">
    <select name="category" class="form-select">
      <option value="">全部类别</option>
      {% for cat in categories %}
      <option value="{{ cat }}" {% if cat == category %}selected{% endif %}>{{ cat }}</option>
      {% endfor %}
    </select>
  </div>
  <div class="col-auto">
    <button type="submit" class="btn btn-secondary">筛选</button>
  </div>
  <div class="col-auto ms-auto">
    <a href="{{ url_for('add_item') }}" class="btn btn-primary">新增物品</a>
  </div>
</form>
<table class="table table-hover">
  <thead class="table-light">
    <tr>
      <th>名称</th><th>类别</th><th>状态</th><th>存放位置</th><th>负责人</th><th>操作</th>
    </tr>
  </thead>
  <tbody>
    {% for item in items %}
    {%- set row_class = "" -%}
    {% if item.status %}
      {% if '用完' in item.status %}
        {% set row_class = 'table-danger' %}
      {% elif '少量' in item.status %}
        {% set row_class = 'table-warning' %}
      {% elif '充足' in item.status %}
        {% set row_class = 'table-success' %}
      {% endif %}
    {% endif %}
    <tr class="{{ row_class }}">
      <td><a href="{{ url_for('item_detail', item_id=item.id) }}">{{ item.name }}</a></td>
      <td>{{ item.category or '' }}</td>
      <td>{{ item.status or '' }}</td>
      <td>
        {% if item.location %}
          <a href="{{ url_for('view_location', loc_id=item.location.id) }}">{{ item.location.name }}</a>
        {% else %}-{% endif %}
      </td>
      <td>
        {% if item.responsible_member %}
          <a href="{{ url_for('profile', member_id=item.responsible_member.id) }}">{{ item.responsible_member.name }}</a>
        {% else %}-{% endif %}
      </td>
      <td>
        <a href="{{ url_for('edit_item', item_id=item.id) }}" class="btn btn-sm btn-outline-primary">编辑</a>
        <form action="{{ url_for('delete_item', item_id=item.id) }}" method="post" class="d-inline" onsubmit="return confirm('确定删除该物品吗？');">
          <button type="submit" class="btn btn-sm btn-outline-danger">删除</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    {% if items|length == 0 %}
    <tr><td colspan="6" class="text-center text-muted">没有找到物品</td></tr>
    {% endif %}
  </tbody>
</table>
{% endblock %}

templates/item_form.html

{% extends "base.html" %}
{% block content %}
<h4 class="mb-3">{{ item and item.id and '编辑物品' or '新增物品' }}</h4>
<form method="post" enctype="multipart/form-data" action="{% if item %}{{ url_for('edit_item', item_id=item.id) }}{% else %}{{ url_for('add_item') }}{% endif %}">
  <div class="mb-3">
    <label for="name" class="form-label">物品名称</label>
    <input type="text" class="form-control" id="name" name="name" value="{{ item.name if item else '' }}" required>
  </div>
  <div class="mb-3">
    <label for="category" class="form-label">类别</label>
    <input type="text" class="form-control" id="category" name="category" value="{{ item.category if item else '' }}">
  </div>
  <div class="mb-3">
    <label for="status" class="form-label">状态</label>
    <select id="status" name="status" class="form-select">
      <option value="" {% if not item or not item.status %}selected{% endif %}>--选择状态--</option>
      <optgroup label="库存状态">
        <option value="充足" {% if item and item.status == '充足' %}selected{% endif %}>充足</option>
        <option value="少量" {% if item and item.status == '少量' %}selected{% endif %}>少量</option>
        <option value="用完" {% if item and item.status == '用完' %}selected{% endif %}>用完</option>
      </optgroup>
      <optgroup label="物品特性">
        <option value="安全" {% if item and item.status == '安全' %}selected{% endif %}>安全</option>
        <option value="一般" {% if item and item.status == '一般' %}selected{% endif %}>一般</option>
        <option value="危险" {% if item and item.status == '危险' %}selected{% endif %}>危险</option>
        <option value="昂贵" {% if item and item.status == '昂贵' %}selected{% endif %}>昂贵</option>
      </optgroup>
      {% if item and item.status not in ['充足','少量','用完','安全','一般','危险','昂贵'] and item.status %}
      <!-- 保留编辑时的其他状态选项 -->
      <option value="{{ item.status }}" selected>{{ item.status }}</option>
      {% endif %}
    </select>
  </div>
  <div class="mb-3">
    <label for="responsible" class="form-label">负责人</label>
    <select id="responsible" name="responsible_id" class="form-select">
      <option value="">(未指定)</option>
      {% for m in members %}
      <option value="{{ m.id }}" {% if item and item.responsible_id == m.id %}selected{% endif %}>{{ m.name }}</option>
      {% endfor %}
    </select>
  </div>
  <div class="mb-3">
    <label for="location" class="form-label">存放位置</label>
    <select id="location" name="location_id" class="form-select">
      <option value="">(未指定)</option>
      {% for loc in locations %}
      <option value="{{ loc.id }}" {% if item and item.location_id == loc.id %}selected{% endif %}>{{ loc.name }}</option>
      {% endfor %}
    </select>
  </div>
  <div class="mb-3">
    <label for="purchase_link" class="form-label">购买链接</label>
    <input type="url" class="form-control" id="purchase_link" name="purchase_link" value="{{ item.purchase_link if item else '' }}">
  </div>
  <div class="mb-3">
    <label for="imageInput" class="form-label">物品图片 (可选)</label>
    {% if item and item.image %}
    <div class="mb-2">
      <img src="{{ url_for('static', filename='images/' + item.image) }}" alt="物品图片" class="img-thumbnail" style="max-width:150px;">
    </div>
    {% endif %}
    <input type="file" id="imageInput" name="image" accept="image/*" capture="environment" class="form-control">
    <img id="imgPreview" src="#" alt="预览" style="max-width: 120px; display: none; margin-top: 5px;">
  </div>
  <div class="mb-3">
    <label for="notes" class="form-label">备注</label>
    <textarea class="form-control" id="notes" name="notes" rows="3">{{ item.notes if item else '' }}</textarea>
  </div>
  <button type="submit" class="btn btn-success">{{ item and '保存修改' or '添加物品' }}</button>
  <a href="{{ url_for('items') }}" class="btn btn-secondary">取消</a>
</form>
<script>
  // 图片预览脚本
  const fileInput = document.getElementById('imageInput');
  const previewImg = document.getElementById('imgPreview');
  if(fileInput && previewImg) {
    fileInput.addEventListener('change', function(e) {
      const file = e.target.files[0];
      if(file) {
        previewImg.src = URL.createObjectURL(file);
        previewImg.style.display = 'block';
      } else {
        previewImg.style.display = 'none';
      }
    });
  }
</script>
{% endblock %}

templates/item_detail.html

{% extends "base.html" %}
{% block content %}
<h4 class="mb-3">物品详情</h4>
<div class="row">
  <div class="col-md-6">
    <ul class="list-group mb-3">
      <li class="list-group-item"><strong>名称:</strong> {{ item.name }}</li>
      <li class="list-group-item"><strong>类别:</strong> {{ item.category or '未分类' }}</li>
      <li class="list-group-item"><strong>状态:</strong> {{ item.status or '未指定' }}</li>
      <li class="list-group-item"><strong>负责人:</strong>
        {% if item.responsible_member %}
          <a href="{{ url_for('profile', member_id=item.responsible_member.id) }}">{{ item.responsible_member.name }}</a>
        {% else %} 无 {% endif %}
      </li>
      <li class="list-group-item"><strong>存放位置:</strong>
        {% if item.location %}
          <a href="{{ url_for('view_location', loc_id=item.location.id) }}">{{ item.location.name }}</a>
        {% else %} 未指定 {% endif %}
      </li>
      <li class="list-group-item"><strong>最近更新时间:</strong> {{ item.last_modified.strftime("%Y-%m-%d %H:%M:%S") if item.last_modified else '' }}</li>
      {% if item.purchase_link %}
      <li class="list-group-item"><strong>购买链接:</strong> <a href="{{ item.purchase_link }}" target="_blank">点击打开</a></li>
      {% endif %}
      {% if item.notes %}
      <li class="list-group-item"><strong>备注:</strong><br>{{ item.notes }}</li>
      {% endif %}
    </ul>
    <a href="{{ url_for('edit_item', item_id=item.id) }}" class="btn btn-primary">编辑</a>
    <form action="{{ url_for('delete_item', item_id=item.id) }}" method="post" class="d-inline" onsubmit="return confirm('确定删除该物品吗？');">
      <button type="submit" class="btn btn-danger">删除</button>
    </form>
    <a href="{{ url_for('items') }}" class="btn btn-secondary">返回列表</a>
  </div>
  <div class="col-md-6">
    {% if item.image %}
      <img src="{{ url_for('static', filename='images/' + item.image) }}" alt="物品图片" class="img-fluid img-thumbnail">
    {% else %}
      <p class="text-muted">无图片</p>
    {% endif %}
  </div>
</div>
{% endblock %}

templates/locations.html

{% extends "base.html" %}
{% block content %}
<h3 class="mb-3">实验室位置列表</h3>
<a href="{{ url_for('add_location') }}" class="btn btn-primary mb-3">新增位置</a>
<table class="table table-hover">
  <thead class="table-light">
    <tr><th>名称</th><th>负责人</th><th>包含物品数量</th><th>操作</th></tr>
  </thead>
  <tbody>
    {% for loc in locations %}
    <tr>
      <td><a href="{{ url_for('view_location', loc_id=loc.id) }}">{{ loc.name }}</a></td>
      <td>
        {% if loc.responsible_member %}
          <a href="{{ url_for('profile', member_id=loc.responsible_member.id) }}">{{ loc.responsible_member.name }}</a>
        {% else %} - {% endif %}
      </td>
      <td>{{ loc.items|length }}</td>
      <td>
        <a href="{{ url_for('edit_location', loc_id=loc.id) }}" class="btn btn-sm btn-outline-primary">编辑</a>
        <form action="{{ url_for('delete_location', loc_id=loc.id) }}" method="post" class="d-inline" onsubmit="return confirm('确定删除该位置吗？');">
          <button type="submit" class="btn btn-sm btn-outline-danger">删除</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    {% if locations|length == 0 %}
    <tr><td colspan="4" class="text-center text-muted">暂无位置记录</td></tr>
    {% endif %}
  </tbody>
</table>
{% endblock %}

templates/location_form.html

{% extends "base.html" %}
{% block content %}
<h4 class="mb-3">{{ location and '编辑位置' or '新增位置' }}</h4>
<form method="post" enctype="multipart/form-data" action="{% if location %}{{ url_for('edit_location', loc_id=location.id) }}{% else %}{{ url_for('add_location') }}{% endif %}">
  <div class="mb-3">
    <label for="name" class="form-label">位置名称</label>
    <input type="text" class="form-control" id="name" name="name" value="{{ location.name if location else '' }}" required>
  </div>
  <div class="mb-3">
    <label for="responsible" class="form-label">负责人</label>
    <select id="responsible" name="responsible_id" class="form-select">
      <option value="">(未指定)</option>
      {% for m in members %}
      <option value="{{ m.id }}" {% if location and location.responsible_id == m.id %}selected{% endif %}>{{ m.name }}</option>
      {% endfor %}
    </select>
  </div>
  <div class="mb-3">
    <label for="imageInput" class="form-label">位置图片 (可选)</label>
    {% if location and location.image %}
    <div class="mb-2">
      <img src="{{ url_for('static', filename='images/' + location.image) }}" alt="位置图片" class="img-thumbnail" style="max-width:150px;">
    </div>
    {% endif %}
    <input type="file" id="imageInput" name="image" accept="image/*" capture="environment" class="form-control">
    <img id="imgPreview" src="#" alt="预览" style="max-width: 120px; display: none; margin-top: 5px;">
  </div>
  <div class="mb-3">
    <label for="notes" class="form-label">备注</label>
    <textarea class="form-control" id="notes" name="notes" rows="3">{{ location.notes if location else '' }}</textarea>
  </div>
  <button type="submit" class="btn btn-success">{{ location and '保存修改' or '添加位置' }}</button>
  <a href="{{ url_for('locations_list') }}" class="btn btn-secondary">取消</a>
</form>
<script>
  // 图片预览脚本
  const fileInput2 = document.getElementById('imageInput');
  const previewImg2 = document.getElementById('imgPreview');
  if(fileInput2 && previewImg2) {
    fileInput2.addEventListener('change', function(e) {
      const file = e.target.files[0];
      if(file) {
        previewImg2.src = URL.createObjectURL(file);
        previewImg2.style.display = 'block';
      } else {
        previewImg2.style.display = 'none';
      }
    });
  }
</script>
{% endblock %}

templates/location.html

{% extends "base.html" %}
{% block content %}
<h4 class="mb-3">{{ location.name }}</h4>
<p>负责人:
  {% if location.responsible_member %}
    <a href="{{ url_for('profile', member_id=location.responsible_member.id) }}">{{ location.responsible_member.name }}</a>
  {% else %} 无 {% endif %}
  {% if location.notes %}
  <br><small class="text-muted">{{ location.notes }}</small>
  {% endif %}
</p>
{% if location.image %}
  <div class="mb-3">
    <div class="position-relative border" style="display: inline-block;">
      <img id="locationImg" src="{{ url_for('static', filename='images/' + location.image) }}" alt="{{ location.name }}" class="img-fluid">
      {% for it in items %}
        {% if it.pos_x is not none and it.pos_y is not none %}
        <a href="{{ url_for('item_detail', item_id=it.id) }}" class="pin-icon" style="left: {{ it.pos_x }}%; top: {{ it.pos_y }}%;" title="{{ it.name }}">📌</a>
        {% endif %}
      {% endfor %}
    </div>
  </div>
  {% set items_no_pos = items|selectattr("pos_x", "==", none) | list %}
  {% if items_no_pos %}
  <form method="post" action="{{ url_for('set_item_position', loc_id=location.id) }}" class="mb-4">
    <div class="row align-items-center">
      <div class="col-auto">
        <label for="itemSelect" class="form-label mb-0">将物品标记在图中:</label>
      </div>
      <div class="col-auto">
        <select id="itemSelect" name="item_id" class="form-select">
          {% for it in items_no_pos %}
          <option value="{{ it.id }}">{{ it.name }}</option>
          {% endfor %}
        </select>
      </div>
      <input type="hidden" name="pos_x" id="posX">
      <input type="hidden" name="pos_y" id="posY">
      <div class="col-auto">
        <button type="submit" class="btn btn-primary">保存位置</button>
      </div>
      <div class="col-12">
        <small class="text-muted">点击上方图片选择位置，然后点击保存</small>
      </div>
    </div>
  </form>
  <script>
    // 点击图片获取坐标脚本
    const locImg = document.getElementById('locationImg');
    if(locImg) {
      locImg.addEventListener('click', function(e) {
        const rect = locImg.getBoundingClientRect();
        const xPercent = ((e.clientX - rect.left) / rect.width) * 100;
        const yPercent = ((e.clientY - rect.top) / rect.height) * 100;
        const posXInput = document.getElementById('posX');
        const posYInput = document.getElementById('posY');
        if(posXInput && posYInput) {
          posXInput.value = xPercent.toFixed(2);
          posYInput.value = yPercent.toFixed(2);
        }
      });
    }
  </script>
  {% endif %}
{% else %}
  <p class="text-muted">无图片</p>
{% endif %}
<h5 class="mt-4">包含的物品</h5>
<ul class="list-group">
  {% for it in items %}
    <li class="list-group-item">
      {% if it.pos_x is not none and it.pos_y is not none %}📌 {% endif %}
      <a href="{{ url_for('item_detail', item_id=it.id) }}">{{ it.name }}</a>
      {% if it.status %}<span class="badge bg-secondary">{{ it.status }}</span>{% endif %}
      {% if it.responsible_member %}<small class="text-muted"> - {{ it.responsible_member.name }}</small>{% endif %}
    </li>
  {% else %}
    <li class="list-group-item text-muted">（此位置暂无物品）</li>
  {% endfor %}
</ul>
{% endblock %}

templates/members.html

{% extends "base.html" %}
{% block content %}
<h3 class="mb-3">课题组成员</h3>
<table class="table table-hover">
  <thead class="table-light">
    <tr><th>姓名</th><th>用户名</th><th>联系方式</th><th>负责物品数</th><th>负责位置数</th></tr>
  </thead>
  <tbody>
    {% for mem in members %}
    <tr>
      <td><a href="{{ url_for('profile', member_id=mem.id) }}">{{ mem.name }}</a></td>
      <td>{{ mem.username }}</td>
      <td>{{ mem.contact or '-' }}</td>
      <td>{{ mem.items|length }}</td>
      <td>{{ mem.locations|length }}</td>
    </tr>
    {% endfor %}
    {% if members|length == 0 %}
    <tr><td colspan="5" class="text-center text-muted">暂无成员</td></tr>
    {% endif %}
  </tbody>
</table>
{% endblock %}

templates/profile.html

{% extends "base.html" %}
{% block content %}
<div class="mb-4">
  <div class="row">
    <div class="col-auto">
      {% if profile_user.photo %}
      <img src="{{ url_for('static', filename='images/' + profile_user.photo) }}" alt="头像" class="img-thumbnail" style="width:100px; height:100px; object-fit: cover;">
      {% else %}
      <div class="bg-secondary text-white d-flex align-items-center justify-content-center rounded" style="width:100px; height:100px;">
        <span style="font-size:50px;">&#128100;</span>
      </div>
      {% endif %}
    </div>
    <div class="col">
      <h4>{{ profile_user.name }}</h4>
      <p>
        {% if profile_user.contact %}📞 {{ profile_user.contact }}<br>{% endif %}
        {% if profile_user.notes %}<small class="text-muted">{{ profile_user.notes }}</small>{% endif %}
      </p>
    </div>
  </div>
</div>
<div class="row">
  <div class="col-md-6">
    <h5>负责的物品</h5>
    <ul class="list-group mb-4">
      {% for it in items_resp %}
      <li class="list-group-item">
        <a href="{{ url_for('item_detail', item_id=it.id) }}">{{ it.name }}</a>
        {% if it.status %}<span class="badge bg-secondary">{{ it.status }}</span>{% endif %}
      </li>
      {% else %}
      <li class="list-group-item text-muted">无负责物品</li>
      {% endfor %}
    </ul>
  </div>
  <div class="col-md-6">
    <h5>负责的位置</h5>
    <ul class="list-group mb-4">
      {% for loc in locations_resp %}
      <li class="list-group-item">
        <a href="{{ url_for('view_location', loc_id=loc.id) }}">{{ loc.name }}</a>
      </li>
      {% else %}
      <li class="list-group-item text-muted">无负责位置</li>
      {% endfor %}
    </ul>
  </div>
</div>
{% if current_user.id == profile_user.id %}
<div class="mb-4">
  <h5>通知</h5>
  <ul class="list-group">
    {% for log in notifications %}
    {% set verb = '进行了操作' %}
    {% if '新增' in log.action_type %}{% set verb = '添加了' %}{% elif '修改' in log.action_type %}{% set verb = '修改了' %}{% elif '删除' in log.action_type %}{% set verb = '删除了' %}{% endif %}
    <li class="list-group-item">
      你的{% if log.item_id %}物品{% elif log.location_id %}位置{% endif %}
      <strong>
      {% if log.item_id %}<a href="{{ url_for('item_detail', item_id=log.item.id) }}">{{ log.item.name }}</a>
      {% elif log.location_id %}<a href="{{ url_for('view_location', loc_id=log.location.id) }}">{{ log.location.name }}</a>
      {% endif %}
      </strong>
      被 {{ log.user.name if log.user else '某人' }} {{ verb }}
      <small class="text-muted">({{ log.timestamp.strftime("%Y-%m-%d %H:%M") }})</small>
    </li>
    {% else %}
    <li class="list-group-item text-muted">暂无通知</li>
    {% endfor %}
  </ul>
</div>
<div class="mb-4">
  <h5>我的操作记录</h5>
  <ul class="list-group">
    {% for log in user_logs %}
    <li class="list-group-item">
      <small class="text-muted">{{ log.timestamp.strftime("%Y-%m-%d %H:%M") }}</small>
      {% if log.item %}
        - {{ log.action_type }}: <a href="{{ url_for('item_detail', item_id=log.item.id) }}">{{ log.item.name }}</a>
      {% elif log.location %}
        - {{ log.action_type }}: <a href="{{ url_for('view_location', loc_id=log.location.id) }}">{{ log.location.name }}</a>
      {% else %}
        - {{ log.action_type }}
      {% endif %}
    </li>
    {% else %}
    <li class="list-group-item text-muted">暂无操作记录</li>
    {% endfor %}
  </ul>
</div>
{% endif %}
<div class="mb-4">
  <h5>留言板</h5>
  <ul class="list-group mb-3">
    {% for msg in messages %}
    <li class="list-group-item">
      <strong>{{ msg.sender.name if msg.sender else '匿名' }}:</strong> {{ msg.content }}
      <br><small class="text-muted">{{ msg.timestamp.strftime("%Y-%m-%d %H:%M") }}</small>
    </li>
    {% else %}
    <li class="list-group-item text-muted">暂无留言</li>
    {% endfor %}
  </ul>
  <form method="post" action="{{ url_for('post_message', member_id=profile_user.id) }}">
    <div class="mb-3">
      <label for="newMessage" class="form-label">留言:</label>
      <textarea id="newMessage" name="content" class="form-control" rows="2" required></textarea>
    </div>
    <button type="submit" class="btn btn-primary">发布留言</button>
  </form>
</div>
{% endblock %}

templates/edit_profile.html

{% extends "base.html" %}
{% block content %}
<h4 class="mb-3">编辑个人信息</h4>
<form method="post" enctype="multipart/form-data" action="{{ url_for('edit_profile', member_id=member.id) }}">
  <div class="mb-3">
    <label for="name" class="form-label">姓名</label>
    <input type="text" class="form-control" id="name" name="name" value="{{ member.name }}" required>
  </div>
  <div class="mb-3">
    <label for="contact" class="form-label">联系方式</label>
    <input type="text" class="form-control" id="contact" name="contact" value="{{ member.contact or '' }}">
  </div>
  <div class="mb-3">
    <label for="notes" class="form-label">个人展示/备注</label>
    <textarea class="form-control" id="notes" name="notes" rows="3">{{ member.notes or '' }}</textarea>
  </div>
  <div class="mb-3">
    <label for="password" class="form-label">新密码 (留空则不修改)</label>
    <input type="password" class="form-control" id="password" name="password">
  </div>
  <div class="mb-3">
    <label for="photoInput" class="form-label">头像 (可选)</label>
    {% if member.photo %}
    <div class="mb-2">
      <img src="{{ url_for('static', filename='images/' + member.photo) }}" alt="头像" class="img-thumbnail" style="max-width:100px;">
    </div>
    {% endif %}
    <input type="file" id="photoInput" name="photo" accept="image/*" capture="user" class="form-control">
    <img id="photoPreview" src="#" alt="预览" style="max-width: 100px; display: none; margin-top: 5px;">
  </div>
  <button type="submit" class="btn btn-success">保存</button>
  <a href="{{ url_for('profile', member_id=member.id) }}" class="btn btn-secondary">取消</a>
</form>
<script>
  // 头像预览脚本
  const photoInput = document.getElementById('photoInput');
  const photoPreview = document.getElementById('photoPreview');
  if(photoInput && photoPreview) {
    photoInput.addEventListener('change', function(e) {
      const file = e.target.files[0];
      if(file) {
        photoPreview.src = URL.createObjectURL(file);
        photoPreview.style.display = 'block';
      } else {
        photoPreview.style.display = 'none';
      }
    });
  }
</script>
{% endblock %}





以下是你完整 化学实验室管理系统 项目的 requirements.txt 文件内容，以及在 macOS（如 Mac mini M4） 上运行项目所需的终端命令行指令。

⸻

✅ requirements.txt

Flask>=3.0.0
Flask-Login>=0.6.3
Flask-SQLAlchemy>=3.1.1
Werkzeug>=3.0.1
pandas>=2.2.2

📌 所有版本为 2025 年最新稳定版本（Flask 3.x、pandas 2.x 等），兼容 Python 3.10+。

⸻

✅ macOS 环境下运行指令（推荐使用虚拟环境）

🔹 1. 创建项目目录（例如放在桌面）

cd ~/Desktop
mkdir chem_lab_system
cd chem_lab_system

🔹 2. 建立虚拟环境并激活（推荐）

python3 -m venv venv
source venv/bin/activate

🔹 3. 创建并保存 requirements.txt 文件

nano requirements.txt

将上方内容粘贴进去，保存并退出（Ctrl + O → 回车，Ctrl + X）。

🔹 4. 安装依赖

pip install -r requirements.txt

🔹 5. 创建目录结构（如上传目录）

mkdir -p static/images templates

将我上面给出的 Python 文件（如 app.py）、HTML 模板（如 templates/base.html）放入对应路径。

🔹 6. 初始化数据库（首次运行自动）

python app.py

终端将输出：

 * Running on http://127.0.0.1:5000

浏览器访问：
👉 http://127.0.0.1:5000
初始默认账号为：

用户名：admin
密码：admin


⸻

🔒 其他建议

功能	说明
上传图片大小限制	默认最大 16MB，可通过 app.config['MAX_CONTENT_LENGTH'] 调整
静态文件路径	所有上传图片保存在 static/images/ 目录
数据备份导出	支持通过 /export/items 等路径导出 CSV 文件
生产部署建议	使用 gunicorn + Nginx 实现后端部署


⸻

如果你需要我打包为 .zip 或 .tar.gz 项目结构、添加一键运行脚本、或配置 systemd 部署指令，也可以告诉我！我可以直接帮你生成。
# Exact lifecycle excerpts from pyhwpx 1.7.2 core.py (MIT).
# Wheel c79d6aa67baae9cd82fd7ae11217e985fa2fe832c61a09a2715aae854a0d1349.
# Test-only: executed with injected SDK/COM dependencies, never imports Hancom.
class Hwp:
    def __init__(
            self,
            new: bool = False,
            visible: bool = True,
            register_module: bool = True,
            on_quit: bool = False,
    ):
        self.hwp = 0
        self.on_quit = on_quit
        self.htf_fonts = fonts
        context = pythoncom.CreateBindCtx(0)
        pythoncom.CoInitialize()  # 이걸 꼭 실행해야 하는가? 왜 Pycharm이나 주피터에서는 괜찮고, vscode에서는 CoInitialize 오류가 나는지?
        running_coms = pythoncom.GetRunningObjectTable()
        monikers = running_coms.EnumRunning()

        if not new:
            for moniker in monikers:
                name = moniker.GetDisplayName(context, moniker)
                if name.startswith("!HwpObject."):
                    obj = running_coms.GetObject(moniker)
                    self.hwp = win32.gencache.EnsureDispatch(
                        obj.QueryInterface(pythoncom.IID_IDispatch)
                    )
        if not self.hwp:
            self.hwp = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
        try:
            self.hwp.XHwpWindows.Active_XHwpWindow.Visible = visible
        except Exception as e:
            # print(e)
            sleep(0.01)
            self.hwp = win32.gencache.EnsureDispatch("HWPFrame.HwpObject")
            self.hwp.XHwpWindows.Active_XHwpWindow.Visible = visible

        if register_module:  # and not check_registry_key():
            try:
                self.register_module()
            except Exception as e:
                print(
                    e, "RegisterModule 액션을 실행할 수 없음. 개발자에게 문의해주세요."
                )

    def __del__(self):
        if self.on_quit:
            try:
                self.quit(save=False)
            except:
                pass
        pythoncom.CoUninitialize()

    @property
    def IsModified(self) -> bool:
        """
        최근 저장 또는 생성 이후 수정이 있는지 여부를 나타낸다. 읽기전용이며,
        자동화에 활용하는 경우는 거의 없다. 패스~

        Examples:
            >>> from pyhwpx import Hwp
            >>> hwp = Hwp()
            >>> hwp.open("./example.hwpx")
            >>> # 신나게 작업을 마친 후, 종료하기 직전, 혹시 하는 마음에
            >>> # 수정사항이 있으면 저장하고 끄기 & 수정사항이 없으면 그냥 한/글 종료하기
            >>> if hwp.IsModified:
            ...     hwp.save()
            ... hwp.quit()
        """
        return self.hwp.IsModified

    @property
    def Path(self) -> str:
        """
        현재 빈 문서가 아닌 경우, 열려 있는 문서의 파일명을 포함한 전체경로를 리턴한다.

        Returns:
        현재 문서의 전체경로

        Examples:
            >>> from pyhwpx import Hwp
            >>> hwp = Hwp()
            >>> hwp.open("./example.hwpx")
            >>> hwp.Path
            C:/Users/User/desktop/example.hwpx
        """
        return self.hwp.Path

    @property
    def XHwpDocuments(self):
        """
        HwpApplication의 XHwpDocuments 객체를 리턴한다.

        Returns:
        """
        return self.hwp.XHwpDocuments

    @property
    def XHwpWindows(self) -> "Hwp.XHwpWindows":
        return self.hwp.XHwpWindows

    def clear(self, option: int = 1) -> None:
        """
        현재 편집중인 문서의 내용을 닫고 빈문서 편집 상태로 돌아간다.

        Args:
            option: 편집중인 문서의 내용에 대한 처리 방법, 생략하면 1(hwpDiscard)가 선택된다.

                - 0: 문서의 내용이 변경되었을 때 사용자에게 저장할지 묻는 대화상자를 띄운다. (hwpAskSave)
                - 1: 문서의 내용을 버린다. (hwpDiscard, 기본값)
                - 2: 문서가 변경된 경우 저장한다. (hwpSaveIfDirty)
                - 3: 무조건 저장한다. (hwpSave)

        Returns:
            None

        Examples:
            >>> from pyhwpx import Hwp
            >>> hwp = Hwp()
            >>> hwp.clear()
        """
        return self.hwp.XHwpDocuments.Active_XHwpDocument.Clear(option=option)

    def quit(self, save: bool = False) -> None:
        """
        한/글을 종료한다.

        단, 저장되지 않은 변경사항이 있는 경우 팝업이 뜨므로
        clear나 save 등의 메서드를 실행한 후에 quit을 실행해야 한다.

        Args:
            save: 변경사항이 있는 경우 저장할지 여부. 기본값은 저장안함(False)

        Returns:
            None
        """
        if self.Path == "":  # 빈 문서인 경우
            self.clear()
        elif save:  # 빈 문서가 아닌 경우
            self.save()
        else:
            self.clear()
        self.hwp.Quit()

    def save(self, save_if_dirty: bool = True) -> bool:
        """
        현재 편집중인 문서를 저장한다.

        문서의 경로가 지정되어있지 않으면 “새 이름으로 저장” 대화상자가 뜬다.

        Args:
            save_if_dirty:
                True를 지정하면 문서가 변경된 경우에만 저장한다.
                False를 지정하면 변경여부와 상관없이 무조건 저장한다.
                생략하면 True가 지정된다.

        Returns:
            성공하면 True, 실패하면 False
        """
        return self.hwp.Save(save_if_dirty=save_if_dirty)
